import streamlit as st
import os
from supabase import create_client, Client
import pandas as pd
from datetime import datetime, timedelta
from woocommerce import API
import requests
import time

# Configurare pagină
st.set_page_config(
    page_title="ServicePack Stock Management",
    page_icon="📦",
    layout="wide"
)

# Încărcare configurație din Streamlit secrets
try:
    SUPABASE_URL = st.secrets["SUPABASE_URL"]
    SUPABASE_KEY = st.secrets["SUPABASE_KEY"]
    WOO_URL = st.secrets["WOO_URL"]
    WOO_CONSUMER_KEY = st.secrets["WOO_CONSUMER_KEY"]
    WOO_CONSUMER_SECRET = st.secrets["WOO_CONSUMER_SECRET"]
    FONEDAY_API_URL = st.secrets["FONEDAY_API_URL"]
    FONEDAY_API_TOKEN = st.secrets["FONEDAY_API_TOKEN"]
    EUR_RON_RATE = float(st.secrets.get("EUR_RON_RATE", "5.1"))
    MIN_PROFIT_MARGIN = float(st.secrets.get("MIN_PROFIT_MARGIN", "0.88"))
    TVA_RATE = float(st.secrets.get("TVA_RATE", "1.21"))
except Exception as e:
    st.error(f"⚠️ Eroare la încărcarea configurației: {e}")
    st.info("Asigură-te că ai completat toate secretele în Streamlit Cloud Settings.")
    st.stop()

# Inițializare Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Inițializare WooCommerce API (READ ONLY)
wcapi = API(
    url=WOO_URL,
    consumer_key=WOO_CONSUMER_KEY,
    consumer_secret=WOO_CONSUMER_SECRET,
    version="wc/v3",
    timeout=30
)


def log_event(event_type: str, message: str, sku: str = None, 
              product_id: str = None, status: str = "info", payload: dict = None):
    """Salvează evenimente în log"""
    try:
        supabase.table("claude_sync_logs").insert({
            "event_type": event_type,
            "sku": sku,
            "product_id": product_id,
            "message": message,
            "status": status,
            "payload": payload
        }).execute()
    except Exception as e:
        print(f"Error logging: {e}")


def calculate_profit_margin(foneday_price_eur: float, woo_price_ron: float) -> float:
    """Calculează marja de profit în procente"""
    cost_ron = foneday_price_eur * EUR_RON_RATE
    selling_price_without_vat = woo_price_ron / TVA_RATE
    ratio = cost_ron / selling_price_without_vat
    profit_margin = (1 - ratio) * 100
    return round(profit_margin, 2)


def is_profitable(foneday_price_eur: float, woo_price_ron: float) -> bool:
    """Verifică dacă produsul e profitabil"""
    cost_ron = foneday_price_eur * EUR_RON_RATE
    selling_price_without_vat = woo_price_ron / TVA_RATE
    ratio = cost_ron / selling_price_without_vat
    return ratio < MIN_PROFIT_MARGIN


def check_foneday_product(sku: str):
    """Verifică produs în Foneday"""
    try:
        headers = {
            "Authorization": f"Bearer {FONEDAY_API_TOKEN}",
            "Content-Type": "application/json"
        }
        response = requests.get(
            f"{FONEDAY_API_URL}/product/{sku}",
            headers=headers,
            timeout=10
        )
        
        if response.status_code == 200:
            data = response.json()
            return data.get("product")
        return None
    except Exception as e:
        return None


def add_to_foneday_cart(sku: str, quantity: int, note: str = None):
    """Adaugă produs în coșul Foneday"""
    try:
        headers = {
            "Authorization": f"Bearer {FONEDAY_API_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {
            "articles": [{
                "sku": sku,
                "quantity": quantity,
                "note": note
            }]
        }
        response = requests.post(
            f"{FONEDAY_API_URL}/shopping-cart-add-items",
            headers=headers,
            json=payload,
            timeout=10
        )
        
        if response.status_code == 200:
            return response.json()
        return None
    except Exception as e:
        return None


def sync_woocommerce_products():
    """Sincronizează stocuri și prețuri din WooCommerce"""
    page = 1
    per_page = 100
    total_synced = 0
    total_errors = 0
    
    progress_bar = st.progress(0)
    status_container = st.empty()
    
    log_event("sync_start", "Începe sincronizarea WooCommerce", status="info")
    
    while True:
        try:
            status_container.info(f"📥 Procesare pagina {page}...")
            
            response = wcapi.get("products", params={"per_page": per_page, "page": page})
            
            if response.status_code != 200:
                st.error(f"❌ Eroare la citirea produselor: {response.status_code}")
                log_event("sync_error", f"Eroare API WooCommerce: {response.status_code}", status="error")
                break
            
            products = response.json()
            
            if not products or len(products) == 0:
                break
            
            for product in products:
                try:
                    sku = product.get("sku")
                    if not sku:
                        continue
                    
                    # Găsește produsul în Supabase
                    result = supabase.table("product_sku").select(
                        "product_id"
                    ).eq("sku", sku).eq("is_primary", True).execute()
                    
                    if not result.data or len(result.data) == 0:
                        continue
                    
                    product_id = result.data[0]["product_id"]
                    stock_quantity = product.get("stock_quantity", 0)
                    regular_price = product.get("regular_price", "0")
                    woo_product_id = product.get("id")
                    
                    # Sincronizează stocul
                    supabase.table("claude_woo_stock").upsert({
                        "product_id": product_id,
                        "sku": sku,
                        "stock_quantity": stock_quantity if stock_quantity is not None else 0,
                        "woo_product_id": woo_product_id,
                        "last_sync_at": datetime.now().isoformat()
                    }, on_conflict="sku").execute()
                    
                    # Sincronizează prețul
                    supabase.table("claude_woo_prices").upsert({
                        "product_id": product_id,
                        "sku": sku,
                        "regular_price": float(regular_price) if regular_price else 0,
                        "woo_product_id": woo_product_id,
                        "last_sync_at": datetime.now().isoformat()
                    }, on_conflict="sku").execute()
                    
                    total_synced += 1
                    
                except Exception as e:
                    total_errors += 1
                    continue
            
            progress_bar.progress(min(page / 20, 0.99))
            page += 1
            time.sleep(0.5)  # Rate limiting
            
        except Exception as e:
            st.error(f"❌ Eroare: {e}")
            log_event("sync_error", f"Eroare în loop: {e}", status="error")
            break
    
    progress_bar.progress(1.0)
    status_container.empty()
    
    log_event("sync_complete", f"Sincronizare completă: {total_synced} produse, {total_errors} erori", status="success")
    
    return total_synced, total_errors


def check_zero_stock_and_add_to_cart():
    """Verifică produse cu stoc zero și adaugă în coșul Foneday"""
    
    status_container = st.empty()
    progress_bar = st.progress(0)
    
    # Găsește toate produsele cu stoc zero
    stock_result = supabase.table("claude_woo_stock").select(
        "*, product_sku!inner(product_id, sku, is_primary, product(name))"
    ).lte("stock_quantity", 0).execute()
    
    if not stock_result.data or len(stock_result.data) == 0:
        status_container.success("✅ Nu există produse cu stoc zero!")
        log_event("foneday_check", "Nu există produse cu stoc zero", status="info")
        return 0, 0, 0
    
    zero_stock_products = stock_result.data
    total_products = len(zero_stock_products)
    added_to_cart = 0
    not_profitable = 0
    not_in_stock = 0
    
    log_event("foneday_check", f"Verificare {total_products} produse cu stoc zero", status="info")
    
    for idx, product_data in enumerate(zero_stock_products):
        try:
            sku = product_data.get("sku")
            product_id = product_data["product_sku"]["product_id"]
            product_name = product_data["product_sku"]["product"]["name"]
            
            status_container.info(f"🔍 Verificare: {product_name} ({idx+1}/{total_products})")
            progress_bar.progress((idx + 1) / total_products)
            
            # Obține toate SKU-urile produsului
            all_skus_result = supabase.table("product_sku").select("sku, is_primary").eq(
                "product_id", product_id
            ).execute()
            
            if not all_skus_result.data:
                continue
            
            all_skus = all_skus_result.data
            
            # Obține prețul WooCommerce
            price_result = supabase.table("claude_woo_prices").select("regular_price").eq(
                "sku", sku
            ).execute()
            
            if not price_result.data or len(price_result.data) == 0:
                continue
            
            woo_price = float(price_result.data[0]["regular_price"])
            
            if woo_price <= 0:
                continue
            
            # Caută la Foneday pe toate SKU-urile
            foneday_options = []
            
            for sku_item in all_skus:
                sku_to_check = sku_item["sku"]
                foneday_product = check_foneday_product(sku_to_check)
                
                if foneday_product and foneday_product.get("instock") == "Y":
                    foneday_price = float(foneday_product.get("price", 0))
                    
                    if foneday_price > 0:
                        foneday_options.append({
                            "sku": sku_to_check,
                            "price": foneday_price,
                            "is_primary": sku_item["is_primary"],
                            "product_data": foneday_product
                        })
                        
                        # Salvează în inventar
                        supabase.table("claude_foneday_inventory").upsert({
                            "product_id": product_id,
                            "sku": sku,
                            "foneday_sku": sku_to_check,
                            "price_eur": foneday_price,
                            "instock": True,
                            "title": foneday_product.get("title"),
                            "quality": foneday_product.get("quality"),
                            "last_checked_at": datetime.now().isoformat()
                        }, on_conflict="sku,foneday_sku").execute()
                
                time.sleep(0.2)  # Rate limiting Foneday API
            
            if not foneday_options:
                not_in_stock += 1
                log_event("foneday_check", f"Nu există stoc la Foneday: {product_name}", sku=sku, status="warning")
                continue
            
            # Sortează: mai întâi după preț, apoi prioritizează canonical
            foneday_options.sort(key=lambda x: (x["price"], not x["is_primary"]))
            best_option = foneday_options[0]
            
            foneday_sku = best_option["sku"]
            foneday_price = best_option["price"]
            
            # Verifică profitabilitate
            if is_profitable(foneday_price, woo_price):
                profit_margin = calculate_profit_margin(foneday_price, woo_price)
                
                # Verifică dacă nu e deja în coș
                existing_cart = supabase.table("claude_foneday_cart").select("id").eq(
                    "sku", sku
                ).eq("foneday_sku", foneday_sku).eq("status", "added_to_cart").execute()
                
                if existing_cart.data and len(existing_cart.data) > 0:
                    continue  # Deja în coș
                
                # Adaugă în coșul Foneday
                cart_result = add_to_foneday_cart(
                    foneday_sku,
                    1,  # Cantitate implicită 1
                    f"Auto-import stoc zero - {product_name}"
                )
                
                if cart_result:
                    # Salvează în tabel
                    supabase.table("claude_foneday_cart").insert({
                        "product_id": product_id,
                        "sku": sku,
                        "foneday_sku": foneday_sku,
                        "quantity": 1,
                        "price_eur": foneday_price,
                        "woo_price_ron": woo_price,
                        "profit_margin": profit_margin,
                        "is_profitable": True,
                        "status": "added_to_cart",
                        "note": f"Import automat - Profit: {profit_margin}%"
                    }).execute()
                    
                    added_to_cart += 1
                    log_event("cart_add", f"Adăugat în coș: {product_name} - Profit: {profit_margin}%", 
                             sku=sku, product_id=product_id, status="success")
                else:
                    log_event("cart_error", f"Eroare adăugare coș: {product_name}", sku=sku, status="error")
            else:
                profit_margin = calculate_profit_margin(foneday_price, woo_price)
                not_profitable += 1
                
                # Salvează ca neprofitabil
                supabase.table("claude_foneday_cart").insert({
                    "product_id": product_id,
                    "sku": sku,
                    "foneday_sku": foneday_sku,
                    "quantity": 1,
                    "price_eur": foneday_price,
                    "woo_price_ron": woo_price,
                    "profit_margin": profit_margin,
                    "is_profitable": False,
                    "status": "not_profitable",
                    "note": f"Neprofitabil - Marjă: {profit_margin}%"
                }).execute()
                
                log_event("foneday_check", f"Neprofitabil: {product_name} - Marjă: {profit_margin}%", 
                         sku=sku, status="warning")
        
        except Exception as e:
            log_event("error", f"Eroare procesare: {e}", status="error")
            continue
    
    progress_bar.progress(1.0)
    status_container.empty()
    
    log_event("foneday_complete", f"Verificare completă: {added_to_cart} adăugate, {not_profitable} neprofitabile, {not_in_stock} fără stoc", status="success")
    
    return added_to_cart, not_profitable, not_in_stock


# SIDEBAR
st.sidebar.title("📦 ServicePack")
st.sidebar.markdown("**Sistem Sincronizare Stocuri**")
st.sidebar.markdown("---")

page = st.sidebar.radio(
    "📋 Navigare",
    ["🏠 Dashboard", "🔄 Import Zilnic", "📊 Stocuri Critice", "🛒 Coș Foneday", "📝 Istoric Log"]
)

st.sidebar.markdown("---")
st.sidebar.caption(f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

if st.sidebar.button("🔄 Reîmprospătare"):
    st.rerun()


# ===== PAGINI =====

if page == "🏠 Dashboard":
    st.title("📊 Dashboard Principal")
    
    st.markdown("### 📈 Statistici Generale")
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        stock_count = supabase.table("claude_woo_stock").select("*", count="exact").gt("stock_quantity", 0).execute()
        st.metric("✅ Cu Stoc", stock_count.count if stock_count.count else 0)
    
    with col2:
        zero_count = supabase.table("claude_woo_stock").select("*", count="exact").lte("stock_quantity", 0).execute()
        st.metric("❌ Stoc Zero", zero_count.count if zero_count.count else 0)
    
    with col3:
        cart_count = supabase.table("claude_foneday_cart").select("*", count="exact").eq("status", "added_to_cart").execute()
        st.metric("🛒 În Coș Foneday", cart_count.count if cart_count.count else 0)
    
    with col4:
        unprofitable = supabase.table("claude_foneday_cart").select("*", count="exact").eq("is_profitable", False).execute()
        st.metric("⚠️ Neprofitabile", unprofitable.count if unprofitable.count else 0)
    
    st.markdown("---")
    
    # Ultimele sincronizări
    st.markdown("### 🕐 Ultima Sincronizare")
    
    last_sync = supabase.table("claude_sync_logs").select("created_at, message").eq(
        "event_type", "sync_complete"
    ).order("created_at", desc=True).limit(1).execute()
    
    if last_sync.data and len(last_sync.data) > 0:
        last_time = pd.to_datetime(last_sync.data[0]["created_at"]).strftime("%Y-%m-%d %H:%M:%S")
        st.info(f"⏰ Ultima sincronizare: **{last_time}**")
        st.caption(last_sync.data[0]["message"])
    else:
        st.warning("⚠️ Nicio sincronizare încă")
    
    st.markdown("---")
    
    # Ultimele evenimente
    st.markdown("### 📋 Ultimele Evenimente")
    
    logs = supabase.table("claude_sync_logs").select("*").order("created_at", desc=True).limit(15).execute()
    
    if logs.data:
        df = pd.DataFrame(logs.data)
        df["created_at"] = pd.to_datetime(df["created_at"]).dt.strftime("%Y-%m-%d %H:%M:%S")
        st.dataframe(
            df[["created_at", "event_type", "sku", "message", "status"]],
            use_container_width=True,
            height=350
        )
    else:
        st.info("Nu există evenimente înregistrate")


elif page == "🔄 Import Zilnic":
    st.title("🔄 Import Zilnic Automat")
    
    st.markdown("""
    ### Ce face această funcție?
    
    **Pasul 1: Sincronizare WooCommerce**
    - 📥 Citește toate produsele din WooCommerce
    - 💾 Salvează stocurile în baza de date
    - 💰 Salvează prețurile în baza de date
    
    **Pasul 2: Verificare Foneday**
    - 🔍 Găsește produse cu stoc zero
    - 🌐 Verifică disponibilitate la Foneday
    - 📊 Calculează profitabilitatea
    - 🛒 Adaugă automat în coș doar produsele profitabile
    
    ⚠️ **Important**: Procesul poate dura 5-15 minute pentru catalog mare!
    """)
    
    st.markdown("---")
    
    col1, col2 = st.columns(2)
    
    with col1:
        run_sync = st.checkbox("✅ Pasul 1: Sincronizează WooCommerce", value=True)
    
    with col2:
        run_foneday = st.checkbox("✅ Pasul 2: Verifică Foneday & Adaugă în Coș", value=True)
    
    st.markdown("---")
    
    if st.button("▶️ ÎNCEPE IMPORT COMPLET", type="primary", use_container_width=True):
        
        start_time = datetime.now()
        
        # PASUL 1: Sincronizare WooCommerce
        if run_sync:
            st.markdown("## 📥 Pasul 1: Sincronizare WooCommerce")
            total_synced, total_errors = sync_woocommerce_products()
            st.success(f"✅ Sincronizare completă: **{total_synced}** produse, **{total_errors}** erori")
            st.markdown("---")
        
        # PASUL 2: Verificare Foneday
        if run_foneday:
            st.markdown("## 🌐 Pasul 2: Verificare Foneday & Adăugare Coș")
            added, not_profitable, not_in_stock = check_zero_stock_and_add_to_cart()
            
            st.success("✅ Verificare Foneday completă!")
            
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("🛒 Adăugate în Coș", added)
            with col2:
                st.metric("⚠️ Neprofitabile", not_profitable)
            with col3:
                st.metric("❌ Fără Stoc", not_in_stock)
        
        # Rezumat final
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()
        
        st.markdown("---")
        st.success(f"🎉 **Import complet finalizat în {duration:.0f} secunde!**")


elif page == "📊 Stocuri Critice":
    st.title("⚠️ Produse cu Stoc Zero")
    
    critical = supabase.table("claude_v_critical_stock").select("*").execute()
    
    if critical.data and len(critical.data) > 0:
        df = pd.DataFrame(critical.data)
        
        st.metric("📊 Total Produse Stoc Zero", len(df))
        
        st.markdown("---")
        
        # Filtre
        col1, col2 = st.columns(2)
        
        with col1:
            show_available = st.checkbox("Doar disponibile la Foneday", value=False)
        
        with col2:
            show_profitable = st.checkbox("Doar profitabile (≥12%)", value=False)
        
        filtered = df.copy()
        
        if show_available:
            filtered = filtered[filtered["foneday_instock"] == True]
        
        if show_profitable:
            filtered = filtered[filtered["profit_margin_percent"] >= 12]
        
        st.dataframe(
            filtered[[
                "sku", "name", "stock_quantity", "woo_price_ron",
                "foneday_sku", "foneday_price_eur", "foneday_instock",
                "profit_margin_percent"
            ]],
            use_container_width=True,
            height=500
        )
    else:
        st.success("✅ Nu există produse cu stoc zero!")


elif page == "🛒 Coș Foneday":
    st.title("🛒 Produse în Coșul Foneday")
    
    cart = supabase.table("claude_foneday_cart").select(
        "*, product(name)"
    ).order("created_at", desc=True).limit(200).execute()
    
    if cart.data and len(cart.data) > 0:
        df = pd.DataFrame(cart.data)
        
        if "product" in df.columns:
            df["product_name"] = df["product"].apply(
                lambda x: x.get("name") if isinstance(x, dict) else ""
            )
        
        # Filtre status
        status_options = df["status"].unique().tolist()
        selected_status = st.multiselect(
            "Filtrează după status",
            options=status_options,
            default=status_options
        )
        
        filtered = df[df["status"].isin(selected_status)]
        
        st.dataframe(
            filtered[[
                "created_at", "sku", "product_name", "foneday_sku",
                "quantity", "price_eur", "woo_price_ron",
                "profit_margin", "is_profitable", "status", "note"
            ]],
            use_container_width=True,
            height=500
        )
        
        # Statistici
        st.markdown("---")
        col1, col2, col3 = st.columns(3)
        
        with col1:
            total_value = (filtered["price_eur"] * filtered["quantity"]).sum()
            st.metric("💰 Valoare Totală (EUR)", f"€{total_value:.2f}")
        
        with col2:
            profitable_df = filtered[filtered["is_profitable"] == True]
            if len(profitable_df) > 0:
                avg_margin = profitable_df["profit_margin"].mean()
                st.metric("📈 Marjă Medie", f"{avg_margin:.2f}%")
            else:
                st.metric("📈 Marjă Medie", "N/A")
        
        with col3:
            total_items = filtered["quantity"].sum()
            st.metric("📦 Total Articole", int(total_items))
    else:
        st.info("Nu există produse în coș")


elif page == "📝 Istoric Log":
    st.title("📝 Istoric Evenimente")
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        event_types = ["Toate"] + ["sync_start", "sync_complete", "foneday_check", "cart_add", "error"]
        selected_event = st.selectbox("Tip Eveniment", event_types)
    
    with col2:
        statuses = ["Toate", "success", "error", "warning", "info"]
        selected_status = st.selectbox("Status", statuses)
    
    with col3:
        limit = st.number_input("Număr rezultate", min_value=10, max_value=500, value=100, step=10)
    
    # Query
    query = supabase.table("claude_sync_logs").select("*")
    
    if selected_event != "Toate":
        query = query.eq("event_type", selected_event)
    
    if selected_status != "Toate":
        query = query.eq("status", selected_status)
    
    logs = query.order("created_at", desc=True).limit(limit).execute()
    
    if logs.data and len(logs.data) > 0:
        df = pd.DataFrame(logs.data)
        df["created_at"] = pd.to_datetime(df["created_at"]).dt.strftime("%Y-%m-%d %H:%M:%S")
        
        st.dataframe(
            df[["created_at", "event_type", "sku", "message", "status"]],
            use_container_width=True,
            height=500
        )
        
        # Export
        if st.button("📥 Exportă CSV"):
            csv = df.to_csv(index=False)
            st.download_button(
                label="⬇️ Descarcă Log-uri",
                data=csv,
                file_name=f"logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv"
            )
    else:
        st.info("Nu există log-uri pentru filtrele selectate")


# Footer
st.sidebar.markdown("---")
st.sidebar.caption("📦 ServicePack Stock Sync v2.0")
st.sidebar.caption("Creat cu ❤️ de Claude & Perplexity")
