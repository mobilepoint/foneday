import streamlit as st
import os
from supabase import create_client, Client
import pandas as pd
from datetime import datetime, timedelta
from woocommerce import API
import requests
import time
import json

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
              product_id: str = None, status: str = "info"):
    """Salvează evenimente în log"""
    try:
        supabase.table("claude_sync_logs").insert({
            "event_type": event_type,
            "sku": sku,
            "product_id": product_id,
            "message": message,
            "status": status
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


def get_foneday_product_by_sku(foneday_sku: str):
    """Obține produs din Foneday după SKU-ul lor"""
    try:
        headers = {
            "Authorization": f"Bearer {FONEDAY_API_TOKEN}",
            "Content-Type": "application/json"
        }
        response = requests.get(
            f"{FONEDAY_API_URL}/product/{foneday_sku}",
            headers=headers,
            timeout=10
        )
        
        if response.status_code == 200:
            data = response.json()
            return data.get("product")
        return None
    except Exception as e:
        return None


def add_to_foneday_cart(foneday_sku: str, quantity: int, note: str = None):
    """Adaugă produs în coșul Foneday folosind SKU-ul lor"""
    try:
        headers = {
            "Authorization": f"Bearer {FONEDAY_API_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {
            "articles": [{
                "sku": foneday_sku,
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


def get_product_info_from_catalog(sku: str):
    """Obține informații produs din catalog (prin view)"""
    try:
        result = supabase.table("v_product_sku").select(
            "product_id, is_primary"
        ).eq("sku", sku).eq("is_primary", True).limit(1).execute()
        
        if result.data and len(result.data) > 0:
            product_id = result.data[0]["product_id"]
            
            product_result = supabase.table("v_product").select("name").eq("id", product_id).limit(1).execute()
            
            if product_result.data and len(product_result.data) > 0:
                return {
                    "product_id": product_id,
                    "name": product_result.data[0]["name"]
                }
            
            return {"product_id": product_id, "name": sku}
        
        return None
    except Exception as e:
        print(f"Error in get_product_info: {e}")
        return None


def get_all_skus_for_sku(sku: str):
    """Obține toate SKU-urile (inclusiv secundare) pentru un SKU dat"""
    try:
        result = supabase.table("v_product_sku").select(
            "product_id"
        ).eq("sku", sku).eq("is_primary", True).limit(1).execute()
        
        if not result.data or len(result.data) == 0:
            return [{"sku": sku, "is_primary": True}]
        
        product_id = result.data[0]["product_id"]
        
        all_skus_result = supabase.table("v_product_sku").select(
            "sku, is_primary"
        ).eq("product_id", product_id).execute()
        
        if all_skus_result.data:
            return all_skus_result.data
        
        return [{"sku": sku, "is_primary": True}]
    except Exception as e:
        print(f"Error in get_all_skus: {e}")
        return [{"sku": sku, "is_primary": True}]


# ============ PASUL 1: Import WooCommerce ============
def step1_import_woocommerce():
    """PASUL 1: Import produse, prețuri și stocuri din WooCommerce"""
    page = 1
    per_page = 100
    total_new = 0
    total_updated = 0
    total_unchanged = 0
    total_errors = 0
    
    progress_bar = st.progress(0)
    status_container = st.empty()
    
    log_event("step1_start", "PASUL 1: Începe import WooCommerce", status="info")
    
    existing_products = {}
    existing_prices = {}
    
    try:
        status_container.info("📂 Citesc datele existente...")
        existing_result = supabase.table("claude_woo_stock").select("sku, stock_quantity").execute()
        if existing_result.data:
            for item in existing_result.data:
                existing_products[item["sku"]] = item.get("stock_quantity", 0)
        
        existing_price_result = supabase.table("claude_woo_prices").select("sku, regular_price").execute()
        if existing_price_result.data:
            for item in existing_price_result.data:
                existing_prices[item["sku"]] = float(item.get("regular_price", 0))
        
        status_container.success(f"✅ Găsite {len(existing_products)} produse existente")
        time.sleep(1)
    except Exception as e:
        log_event("step1_error", f"Eroare la citirea datelor: {e}", status="error")
    
    batch_new_stock = []
    batch_new_price = []
    batch_update_stock = []
    batch_update_price = []
    
    while True:
        try:
            status_container.info(f"📥 PASUL 1: Citesc WooCommerce - pagina {page}...")
            
            response = wcapi.get("products", params={"per_page": per_page, "page": page})
            
            if response.status_code != 200:
                st.error(f"❌ Eroare API WooCommerce: {response.status_code}")
                break
            
            products = response.json()
            
            if not products:
                break
            
            for product in products:
                try:
                    sku = product.get("sku")
                    if not sku:
                        continue
                    
                    product_info = get_product_info_from_catalog(sku)
                    product_id = product_info["product_id"] if product_info else None
                    
                    stock_quantity = product.get("stock_quantity", 0)
                    regular_price = product.get("regular_price", "0")
                    woo_product_id = product.get("id")
                    
                    current_stock = stock_quantity if stock_quantity is not None else 0
                    current_price = float(regular_price) if regular_price else 0
                    
                    is_new = sku not in existing_products
                    stock_changed = not is_new and existing_products[sku] != current_stock
                    price_changed = sku in existing_prices and existing_prices[sku] != current_price
                    
                    if is_new:
                        stock_data = {
                            "sku": sku,
                            "stock_quantity": current_stock,
                            "woo_product_id": woo_product_id,
                            "last_sync_at": datetime.now().isoformat()
                        }
                        if product_id:
                            stock_data["product_id"] = product_id
                        batch_new_stock.append(stock_data)
                        
                        price_data = {
                            "sku": sku,
                            "regular_price": current_price,
                            "woo_product_id": woo_product_id,
                            "last_sync_at": datetime.now().isoformat()
                        }
                        if product_id:
                            price_data["product_id"] = product_id
                        batch_new_price.append(price_data)
                        
                        total_new += 1
                        
                    elif stock_changed or price_changed:
                        if stock_changed:
                            batch_update_stock.append({
                                "sku": sku,
                                "stock_quantity": current_stock,
                                "last_sync_at": datetime.now().isoformat()
                            })
                        
                        if price_changed:
                            batch_update_price.append({
                                "sku": sku,
                                "regular_price": current_price,
                                "last_sync_at": datetime.now().isoformat()
                            })
                        
                        total_updated += 1
                    else:
                        total_unchanged += 1
                    
                except Exception as e:
                    total_errors += 1
                    continue
            
            if page % 5 == 0:
                status_container.warning(f"💾 Salvez...")
                
                if batch_new_stock:
                    try:
                        supabase.table("claude_woo_stock").insert(batch_new_stock).execute()
                        batch_new_stock = []
                    except: pass
                
                if batch_new_price:
                    try:
                        supabase.table("claude_woo_prices").insert(batch_new_price).execute()
                        batch_new_price = []
                    except: pass
                
                if batch_update_stock:
                    for item in batch_update_stock:
                        try:
                            supabase.table("claude_woo_stock").update({
                                "stock_quantity": item["stock_quantity"],
                                "last_sync_at": item["last_sync_at"]
                            }).eq("sku", item["sku"]).execute()
                        except: pass
                    batch_update_stock = []
                
                if batch_update_price:
                    for item in batch_update_price:
                        try:
                            supabase.table("claude_woo_prices").update({
                                "regular_price": item["regular_price"],
                                "last_sync_at": item["last_sync_at"]
                            }).eq("sku", item["sku"]).execute()
                        except: pass
                    batch_update_price = []
            
            progress_bar.progress(min(page / 30, 0.99))
            page += 1
            time.sleep(0.3)
            
        except Exception as e:
            st.error(f"❌ Eroare: {e}")
            break
    
    status_container.warning(f"💾 Finalizare PASUL 1...")
    
    if batch_new_stock:
        try:
            supabase.table("claude_woo_stock").insert(batch_new_stock).execute()
        except: pass
    
    if batch_new_price:
        try:
            supabase.table("claude_woo_prices").insert(batch_new_price).execute()
        except: pass
    
    if batch_update_stock:
        for item in batch_update_stock:
            try:
                supabase.table("claude_woo_stock").update({
                    "stock_quantity": item["stock_quantity"],
                    "last_sync_at": item["last_sync_at"]
                }).eq("sku", item["sku"]).execute()
            except: pass
    
    if batch_update_price:
        for item in batch_update_price:
            try:
                supabase.table("claude_woo_prices").update({
                    "regular_price": item["regular_price"],
                    "last_sync_at": item["last_sync_at"]
                }).eq("sku", item["sku"]).execute()
            except: pass
    
    progress_bar.progress(1.0)
    status_container.empty()
    
    log_event("step1_complete", f"PASUL 1 complet: {total_new} noi, {total_updated} actualizate", status="success")
    
    return total_new, total_updated, total_unchanged, total_errors


# ============ PASUL 2: Import + Normalizare artcode ============
def step2_import_foneday_all_products():
    """PASUL 2: Import toate produsele din Foneday + normalizare artcode"""
    
    progress_bar = st.progress(0)
    status_container = st.empty()
    
    log_event("step2_start", "PASUL 2: Începe import complet Foneday", status="info")
    
    status_container.info("🌐 PASUL 2: Citesc TOATE produsele din Foneday...")
    
    try:
        headers = {
            "Authorization": f"Bearer {FONEDAY_API_TOKEN}",
            "Content-Type": "application/json"
        }
        
        response = requests.get(
            f"{FONEDAY_API_URL}/products",
            headers=headers,
            timeout=60
        )
        
        if response.status_code != 200:
            st.error(f"❌ Eroare API Foneday: {response.status_code}")
            log_event("step2_error", f"Eroare API Foneday: {response.status_code}", status="error")
            return 0
        
        data = response.json()
        products = data.get("products", [])
        
        if not products:
            st.warning("⚠️ Nu s-au găsit produse în Foneday")
            return 0
        
        status_container.success(f"✅ Găsite {len(products)} produse în Foneday")
        time.sleep(1)
        
        batch_size = 100
        total_saved = 0
        total_artcodes_normalized = 0
        
        for i in range(0, len(products), batch_size):
            batch = products[i:i+batch_size]
            batch_data = []
            batch_artcodes = []
            
            for product in batch:
                try:
                    foneday_sku = product.get("sku")
                    artcode_raw = product.get("artcode")
                    
                    batch_data.append({
                        "foneday_sku": foneday_sku,
                        "artcode": artcode_raw,
                        "ean": product.get("ean"),
                        "title": product.get("title"),
                        "instock": product.get("instock"),
                        "suitable_for": product.get("suitable_for"),
                        "category": product.get("category"),
                        "product_brand": product.get("product_brand"),
                        "quality": product.get("quality"),
                        "model_brand": product.get("model_brand"),
                        "model_codes": product.get("model_codes"),
                        "price_eur": float(product.get("price", 0)) if product.get("price") else None,
                        "last_sync_at": datetime.now().isoformat()
                    })
                    
                    if artcode_raw:
                        artcodes_list = []
                        
                        if isinstance(artcode_raw, str):
                            try:
                                artcodes_list = json.loads(artcode_raw)
                            except:
                                artcodes_list = [artcode_raw.strip()]
                        elif isinstance(artcode_raw, list):
                            artcodes_list = artcode_raw
                        else:
                            artcodes_list = [str(artcode_raw)]
                        
                        for artcode_value in artcodes_list:
                            artcode_clean = str(artcode_value).strip().strip('"').strip("'")
                            if artcode_clean:
                                batch_artcodes.append({
                                    "foneday_sku": foneday_sku,
                                    "artcode": artcode_clean
                                })
                
                except Exception as e:
                    continue
            
            if batch_data:
                try:
                    supabase.table("claude_foneday_products").upsert(
                        batch_data,
                        on_conflict="foneday_sku"
                    ).execute()
                    total_saved += len(batch_data)
                except Exception as e:
                    st.error(f"Eroare salvare produse: {e}")
            
            if batch_artcodes:
                try:
                    supabase.table("claude_foneday_artcodes_normalized").upsert(
                        batch_artcodes,
                        on_conflict="foneday_sku,artcode"
                    ).execute()
                    total_artcodes_normalized += len(batch_artcodes)
                except Exception as e:
                    st.error(f"Eroare salvare artcodes: {e}")
            
            status_container.info(f"💾 Salvate {total_saved}/{len(products)} produse, {total_artcodes_normalized} artcodes...")
            progress_bar.progress(total_saved / len(products))
        
        progress_bar.progress(1.0)
        status_container.empty()
        
        log_event("step2_complete", f"PASUL 2 complet: {total_saved} produse, {total_artcodes_normalized} artcodes normalizate", status="success")
        
        return total_saved
        
    except Exception as e:
        st.error(f"❌ Eroare PASUL 2: {e}")
        log_event("step2_error", f"Eroare: {e}", status="error")
        return 0


# ============ PASUL 3: Mapare SKU → artcode (OPTIMIZAT CU PAGINATION) ============
def step3_map_sku_to_artcode():
    """PASUL 3: Mapare SKU-uri optimizată cu pagination"""
    
    progress_bar = st.progress(0)
    status_container = st.empty()
    
    log_event("step3_start", "PASUL 3: Începe mapare SKU → artcode", status="info")
    
    try:
        status_container.info("📂 PASUL 3: Citesc toate SKU-urile din catalog...")
        
        all_my_skus = []
        page_size = 1000
        page = 0
        
        while True:
            result = supabase.table("v_product_sku").select(
                "sku, product_id"
            ).eq("is_primary", True).range(
                page * page_size, 
                (page + 1) * page_size - 1
            ).execute()
            
            if not result.data or len(result.data) == 0:
                break
            
            all_my_skus.extend(result.data)
            page += 1
            
            status_container.info(f"📂 Citite {len(all_my_skus)} SKU-uri din catalog...")
            
            if len(result.data) < page_size:
                break
        
        if not all_my_skus:
            st.warning("Nu există SKU-uri de mapat")
            return 0
        
        status_container.success(f"✅ Total {len(all_my_skus)} SKU-uri în catalog")
        progress_bar.progress(0.3)
        
        status_container.info("📂 Citesc toate artcode-urile Foneday...")
        
        all_artcodes = []
        page = 0
        
        while True:
            result = supabase.table("claude_foneday_artcodes_normalized").select(
                "*"
            ).range(
                page * page_size, 
                (page + 1) * page_size - 1
            ).execute()
            
            if not result.data or len(result.data) == 0:
                break
            
            all_artcodes.extend(result.data)
            page += 1
            
            status_container.info(f"📂 Citite {len(all_artcodes)} artcode-uri Foneday...")
            
            if len(result.data) < page_size:
                break
        
        if not all_artcodes:
            st.warning("Nu există artcode-uri Foneday")
            return 0
        
        status_container.success(f"✅ Total {len(all_artcodes)} artcode-uri Foneday")
        progress_bar.progress(0.6)
        
        status_container.info("🔗 Creez mapări în memorie...")
        
        artcode_dict = {}
        for item in all_artcodes:
            artcode = item["artcode"]
            if artcode not in artcode_dict:
                artcode_dict[artcode] = []
            artcode_dict[artcode].append({
                "foneday_sku": item["foneday_sku"],
                "artcode": item["artcode"]
            })
        
        batch_mappings = []
        for sku_item in all_my_skus:
            my_sku = sku_item["sku"]
            product_id = sku_item["product_id"]
            
            if my_sku in artcode_dict:
                for foneday_match in artcode_dict[my_sku]:
                    batch_mappings.append({
                        "my_sku": my_sku,
                        "foneday_artcode": foneday_match["artcode"],
                        "foneday_sku": foneday_match["foneday_sku"],
                        "product_id": product_id,
                        "mapping_score": 100,
                        "last_verified_at": datetime.now().isoformat()
                    })
        
        status_container.success(f"✅ Create {len(batch_mappings)} mapări în memorie")
        progress_bar.progress(0.8)
        
        if not batch_mappings:
            st.warning("Nu s-au găsit match-uri între SKU-uri și Foneday")
            return 0
        
        status_container.info("💾 Salvez mapări (FĂRĂ să șterg cele vechi)...")
        
        total_saved = 0
        batch_size = 500
        errors = 0
        
        for i in range(0, len(batch_mappings), batch_size):
            batch = batch_mappings[i:i+batch_size]
            try:
                supabase.table("claude_sku_artcode_mapping").upsert(
                    batch,
                    on_conflict="my_sku,foneday_artcode"
                ).execute()
                total_saved += len(batch)
                status_container.info(f"💾 Salvate {total_saved}/{len(batch_mappings)} mapări...")
            except Exception as e:
                st.warning(f"⚠️ Eroare batch {i//batch_size + 1}: {e}")
                errors += 1
                if errors > 5:
                    st.error("Prea multe erori, opresc procesul")
                    break
                continue
        
        progress_bar.progress(1.0)
        status_container.empty()
        
        final_count = supabase.table("claude_sku_artcode_mapping").select("*", count="exact").execute()
        total_in_db = final_count.count if final_count.count else 0
        
        log_event("step3_complete", f"PASUL 3 complet: {total_saved} procesate, {total_in_db} total în DB", status="success")
        
        return total_in_db
        
    except Exception as e:
        st.error(f"❌ Eroare PASUL 3: {e}")
        log_event("step3_error", f"Eroare: {e}", status="error")
        progress_bar.progress(0)
        status_container.empty()
        return 0


# ============ PASUL 4: Verifică stoc (EXCLUDE comenzi în tranzit) ============
def step4_check_stock_and_prices():
    """PASUL 4: Verifică stoc și prețuri - EXCLUDE produse cu comenzi pending"""
    
    progress_bar = st.progress(0)
    status_container = st.empty()
    
    log_event("step4_start", "PASUL 4: Verificare stoc și prețuri Foneday", status="info")
    
    status_container.info("🔍 PASUL 4: Găsesc produse cu stoc zero (exclude comenzi în tranzit)...")
    
    # Citește SKU-urile cu comenzi pending
    pending_orders = supabase.table("claude_foneday_orders_pending").select("sku, quantity").eq("status", "pending").execute()
    
    pending_skus = {}
    if pending_orders.data:
        for order in pending_orders.data:
            sku = order["sku"]
            qty = order["quantity"]
            pending_skus[sku] = pending_skus.get(sku, 0) + qty
    
    if pending_skus:
        st.info(f"📦 Găsite {len(pending_skus)} SKU-uri cu comenzi în tranzit (vor fi excluse)")
    
    zero_stock_result = supabase.table("claude_woo_stock").select("*").lte("stock_quantity", 0).execute()
    
    if not zero_stock_result.data:
        status_container.success("✅ Nu există produse cu stoc zero!")
        return 0, 0
    
    zero_stock_products = zero_stock_result.data
    total_checked = 0
    total_available = 0
    total_skipped_pending = 0
    
    for idx, product_data in enumerate(zero_stock_products):
        my_sku = product_data.get("sku")
        
        # SKIP dacă există comandă pending pentru acest SKU
        if my_sku in pending_skus:
            total_skipped_pending += 1
            status_container.info(f"⏭️ SKIP {my_sku} - Comandă în tranzit: {pending_skus[my_sku]} buc")
            continue
        
        status_container.info(f"🔍 PASUL 4: Verific {idx+1}/{len(zero_stock_products)}: {my_sku}")
        progress_bar.progress((idx + 1) / len(zero_stock_products))
        
        mapping_result = supabase.table("claude_sku_artcode_mapping").select("*").eq(
            "my_sku", my_sku
        ).execute()
        
        if not mapping_result.data:
            continue
        
        for mapping in mapping_result.data:
            foneday_sku = mapping.get("foneday_sku")
            
            if not foneday_sku:
                continue
            
            foneday_product = get_foneday_product_by_sku(foneday_sku)
            
            if foneday_product:
                total_checked += 1
                
                if foneday_product.get("instock") == "Y":
                    total_available += 1
                    
                    try:
                        supabase.table("claude_foneday_inventory").upsert({
                            "product_id": product_data.get("product_id"),
                            "sku": my_sku,
                            "foneday_sku": foneday_sku,
                            "price_eur": float(foneday_product.get("price", 0)),
                            "instock": True,
                            "title": foneday_product.get("title"),
                            "quality": foneday_product.get("quality"),
                            "last_checked_at": datetime.now().isoformat()
                        }, on_conflict="sku,foneday_sku").execute()
                    except: pass
            
            time.sleep(0.2)
    
    progress_bar.progress(1.0)
    status_container.empty()
    
    log_event("step4_complete", f"PASUL 4: {total_checked} verificate, {total_available} disponibile, {total_skipped_pending} skip (pending)", status="success")
    
    return total_checked, total_available


# ============ PASUL 5: Adaugă în coș (PERMITE COMENZI REPETATE) ============
def step5_add_to_cart():
    """PASUL 5: Adaugă în coș Foneday produsele profitabile (2 bucăți)"""
    
    progress_bar = st.progress(0)
    status_container = st.empty()
    
    log_event("step5_start", "PASUL 5: Adăugare în coș Foneday", status="info")
    
    status_container.info("🛒 PASUL 5: Verific produse profitabile...")
    
    inventory_result = supabase.table("claude_foneday_inventory").select("*").eq("instock", True).execute()
    
    if not inventory_result.data:
        status_container.info("Nu există produse disponibile la Foneday")
        return 0, 0
    
    available_products = inventory_result.data
    added_to_cart = 0
    not_profitable = 0
    
    for idx, item in enumerate(available_products):
        my_sku = item.get("sku")
        foneday_sku = item.get("foneday_sku")
        foneday_price = float(item.get("price_eur", 0))
        
        status_container.info(f"🛒 PASUL 5: Verific {idx+1}/{len(available_products)}: {my_sku}")
        progress_bar.progress((idx + 1) / len(available_products))
        
        price_result = supabase.table("claude_woo_prices").select("regular_price").eq("sku", my_sku).execute()
        
        if not price_result.data:
            continue
        
        woo_price = float(price_result.data[0].get("regular_price", 0))
        
        if woo_price <= 0 or foneday_price <= 0:
            continue
        
        if is_profitable(foneday_price, woo_price):
            profit_margin = calculate_profit_margin(foneday_price, woo_price)
            
            cart_result = add_to_foneday_cart(foneday_sku, 2, f"Auto-import - {my_sku}")
            
            if cart_result:
                try:
                    supabase.table("claude_foneday_cart").insert({
                        "product_id": item.get("product_id"),
                        "sku": my_sku,
                        "foneday_sku": foneday_sku,
                        "quantity": 2,
                        "price_eur": foneday_price,
                        "woo_price_ron": woo_price,
                        "profit_margin": profit_margin,
                        "is_profitable": True,
                        "status": "added_to_cart",
                        "note": f"Profit: {profit_margin}% - 2 buc"
                    }).execute()
                    
                    added_to_cart += 1
                    log_event("step5_add", f"Adăugat: {my_sku} - Profit: {profit_margin}%", sku=my_sku, status="success")
                except: pass
        else:
            not_profitable += 1
        
        time.sleep(0.1)
    
    progress_bar.progress(1.0)
    status_container.empty()
    
    log_event("step5_complete", f"PASUL 5 complet: {added_to_cart} adăugate, {not_profitable} neprofitabile", status="success")
    
    return added_to_cart, not_profitable


# ============ FUNCȚIE: Căutare Oportunități Profit (EXCLUDE stoc 0) ============
def find_high_profit_opportunities(min_profit_percent: float):
    """Caută produse cu marjă de profit mare (DOAR cu stoc ≥ 1)"""
    
    progress_bar = st.progress(0)
    status_container = st.empty()
    
    status_container.info("💰 Caut oportunități de profit mare (DOAR produse cu stoc)...")
    
    log_event("opportunities_start", f"Căutare oportunități profit ≥{min_profit_percent}% (stoc ≥1)", status="info")
    
    opportunities = []
    
    try:
        mappings_result = supabase.table("claude_sku_artcode_mapping").select("*").execute()
        
        if not mappings_result.data:
            st.warning("Nu există mapări. Rulează mai întâi PASUL 3.")
            return []
        
        mappings = mappings_result.data
        total_mappings = len(mappings)
        
        for idx, mapping in enumerate(mappings):
            my_sku = mapping.get("my_sku")
            foneday_sku = mapping.get("foneday_sku")
            
            status_container.info(f"💰 Verific {idx+1}/{total_mappings}: {my_sku}")
            progress_bar.progress((idx + 1) / total_mappings)
            
            stock_result = supabase.table("claude_woo_stock").select("stock_quantity").eq("sku", my_sku).execute()
            
            if not stock_result.data:
                continue
            
            current_stock = stock_result.data[0].get("stock_quantity", 0)
            
            if current_stock is None or current_stock <= 0:
                continue
            
            price_result = supabase.table("claude_woo_prices").select("regular_price").eq("sku", my_sku).execute()
            
            if not price_result.data:
                continue
            
            woo_price = float(price_result.data[0].get("regular_price", 0))
            
            if woo_price <= 0:
                continue
            
            foneday_product = get_foneday_product_by_sku(foneday_sku)
            
            if foneday_product and foneday_product.get("instock") == "Y":
                foneday_price = float(foneday_product.get("price", 0))
                
                if foneday_price > 0:
                    profit_margin = calculate_profit_margin(foneday_price, woo_price)
                    
                    if profit_margin >= min_profit_percent:
                        product_info = get_product_info_from_catalog(my_sku)
                        product_name = product_info["name"] if product_info else my_sku
                        
                        opportunities.append({
                            "sku": my_sku,
                            "product_name": product_name,
                            "foneday_sku": foneday_sku,
                            "woo_price_ron": woo_price,
                            "foneday_price_eur": foneday_price,
                            "profit_margin": profit_margin,
                            "current_stock": current_stock,
                            "foneday_title": foneday_product.get("title"),
                            "quality": foneday_product.get("quality")
                        })
                        
                        log_event("opportunity_found", f"Oportunitate: {my_sku} - Stoc: {current_stock} - Profit: {profit_margin}%", sku=my_sku, status="success")
            
            if idx % 10 == 0:
                time.sleep(0.2)
        
        progress_bar.progress(1.0)
        status_container.empty()
        
        log_event("opportunities_complete", f"Găsite {len(opportunities)} oportunități (stoc ≥1) cu profit ≥{min_profit_percent}%", status="success")
        
        return opportunities
        
    except Exception as e:
        st.error(f"❌ Eroare căutare oportunități: {e}")
        log_event("opportunities_error", f"Eroare: {e}", status="error")
        return []


# SIDEBAR
st.sidebar.title("📦 ServicePack")
st.sidebar.markdown("**Sistem 5 Pași + Oportunități**")
st.sidebar.markdown("---")

page = st.sidebar.radio(
    "📋 Navigare",
    [
        "🏠 Dashboard", 
        "🔄 Import Individual (Pași)", 
        "💰 Oportunități Profit", 
        "📊 Stocuri Critice", 
        "🛒 Coș Foneday",
        "🚚 Comenzi în Tranzit",
        "🗺️ Mapări", 
        "📝 Log"
    ]
)

st.sidebar.markdown("---")
st.sidebar.caption(f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

if st.sidebar.button("🔄 Reîmprospătare"):
    st.rerun()


# ===== PAGINI =====

if page == "🏠 Dashboard":
    st.title("📊 Dashboard Principal")
    
    st.markdown("### 📈 Statistici Generale")
    
    col1, col2, col3, col4, col5 = st.columns(5)
    
    with col1:
        try:
            stock_count = supabase.table("claude_woo_stock").select("*", count="exact").gt("stock_quantity", 0).execute()
            st.metric("✅ Cu Stoc", stock_count.count if stock_count.count else 0)
        except:
            st.metric("✅ Cu Stoc", "N/A")
    
    with col2:
        try:
            zero_count = supabase.table("claude_woo_stock").select("*", count="exact").lte("stock_quantity", 0).execute()
            st.metric("❌ Stoc Zero", zero_count.count if zero_count.count else 0)
        except:
            st.metric("❌ Stoc Zero", "N/A")
    
    with col3:
        try:
            foneday_count = supabase.table("claude_foneday_products").select("*", count="exact").execute()
            st.metric("🌐 Produse Foneday", foneday_count.count if foneday_count.count else 0)
        except:
            st.metric("🌐 Produse Foneday", "N/A")
    
    with col4:
        try:
            mapping_count = supabase.table("claude_sku_artcode_mapping").select("*", count="exact").execute()
            st.metric("🗺️ Mapări SKU", mapping_count.count if mapping_count.count else 0)
        except:
            st.metric("🗺️ Mapări SKU", "N/A")
    
    with col5:
        try:
            pending_count = supabase.table("claude_foneday_orders_pending").select("*", count="exact").eq("status", "pending").execute()
            st.metric("🚚 În Tranzit", pending_count.count if pending_count.count else 0)
        except:
            st.metric("🚚 În Tranzit", "N/A")
    
    st.markdown("---")
    
    st.markdown("### 🕐 Ultimele Sincronizări")
    
    try:
        logs = supabase.table("claude_sync_logs").select("*").order("created_at", desc=True).limit(10).execute()
        
        if logs.data:
            df = pd.DataFrame(logs.data)
            df["created_at"] = pd.to_datetime(df["created_at"]).dt.strftime("%Y-%m-%d %H:%M:%S")
            st.dataframe(
                df[["created_at", "event_type", "message", "status"]],
                use_container_width=True,
                height=300
            )
        else:
            st.info("Nu există log-uri")
    except Exception as e:
        st.error(f"Eroare: {e}")


elif page == "🔄 Import Individual (Pași)":
    st.title("🔄 Import Individual - Alege Pașii")
    
    with st.expander("📚 **CITEȘTE MAI ÎNTÂI - Ce Face Fiecare Pas**", expanded=False):
        st.markdown("""
        ### **Pasul 1: 📥 Sincronizare WooCommerce**
        
        **Ce face:**
        - Citește toate produsele din WooCommerce prin API
        - Extrage: SKU, stoc, preț, ID produs
        - Compară cu datele existente în Supabase
        - **Produse noi** → le adaugă
        - **Stoc/preț modificat** → le actualizează
        - **Nemodificat** → le ignoră (eficiență maximă)
        
        **Rezultat:** Tabele `claude_woo_stock` și `claude_woo_prices` actualizate
        
        **Când:** Zilnic sau când modifici ceva în WooCommerce
        
        ---
        
        ### **Pasul 2: 🌐 Import Complet Catalog Foneday**
        
        **Ce face:**
        - Accesează `GET /products` din API Foneday
        - Descarcă **TOATE produsele** disponibile (mii)
        - Salvează: `foneday_sku`, `artcode` (=SKU-ul tău), preț, stoc, etc.
        - **NORMALIZARE artcode**: Dacă artcode e array `["GH82-18850B", "GH82-18835B"]`, extrage fiecare valoare separat
        
        **Rezultat:** 
        - Tabel `claude_foneday_products` = catalog complet
        - Tabel `claude_foneday_artcodes_normalized` = fiecare artcode pe rând separat
        
        **Când:** O dată pe săptămână (catalogul Foneday nu se schimbă zilnic)
        
        ---
        
        ### **Pasul 3: 🗺️ Mapare SKU-uri**
        
        **Ce face:**
        - Ia fiecare SKU din catalogul tău
        - Caută în tabelul normalizat unde `artcode` = SKU-ul tău
        - **Dacă găsește** → creează legătura: `my_sku` ↔ `foneday_artcode` ↔ `foneday_sku`
        
        **Rezultat:** Tabel `claude_sku_artcode_mapping` cu toate legăturile
        
        **Când:** După Pașii 1 și 2, sau când adaugi produse noi
        
        ---
        
        ### **Pasul 4: 🔍 Verificare Stoc & Preț (Stoc Zero)**
        
        **Ce face:**
        - Găsește produsele tale cu stoc zero
        - **EXCLUDE produse cu comenzi în tranzit** (evită dublare comenzi)
        - Pentru fiecare: găsește maparea → verifică prin API Foneday (timp real)
        - **Dacă e disponibil** → salvează în `claude_foneday_inventory`
        
        **Rezultat:** Știi ce produse cu stoc 0 poți reaproviziona
        
        **Când:** Zilnic pentru reaprovizionare
        
        ---
        
        ### **Pasul 5: 🛒 Adăugare Automată în Coș**
        
        **Ce face:**
        - Ia produsele disponibile la Foneday (din inventar)
        - Calculează marja de profit:
          - Cost RON = Preț EUR × 5.1
          - Preț vânzare fără TVA = Preț WooCommerce / 1.21
          - Marjă = (1 - Cost/Preț vânzare) × 100%
        - **Dacă profitabil (≥12%)** → adaugă **2 bucăți** în coșul Foneday
        - **Dacă neprofitabil** → doar salvează în tabel
        
        **Rezultat:** Produse profitabile adăugate automat în coș, tu doar finalizezi comanda
        
        **Când:** După Pasul 4, când vrei să comanzi automat
        
        ---
        
        ### **🎯 Workflow Recomandat:**
        
        **Prima rulare (setup):**
        1. Pasul 1 → Import WooCommerce
        2. Pasul 2 → Import Foneday + Normalizare (durează mai mult)
        3. Pasul 3 → Mapare SKU-uri
        
        **Zilnic (reaprovizionare):**
        1. Pasul 1 → Actualizează stocuri/prețuri
        2. Pasul 4 → Verifică stoc zero (exclude comenzi în tranzit)
        3. Pasul 5 → Adaugă în coș
        4. Mergi la "🚚 Comenzi în Tranzit" → Confirmă comenzile
        
        **Săptămânal (optimizare):**
        - 💰 Oportunități Profit (marjă mare)
        """)
    
    st.markdown("---")
    
    col1, col2, col3, col4, col5 = st.columns(5)
    
    with col1:
        st.markdown("### Pasul 1")
        st.caption("📥 Import WooCommerce")
        if st.button("▶️ Rulează", key="btn_step1", use_container_width=True):
            st.markdown("## 📥 PASUL 1: Import WooCommerce")
            new, updated, unchanged, errors = step1_import_woocommerce()
            st.success(f"✅ Complet: {new} noi, {updated} actualizate")
    
    with col2:
        st.markdown("### Pasul 2")
        st.caption("🌐 Import Foneday")
        if st.button("▶️ Rulează", key="btn_step2", use_container_width=True):
            st.markdown("## 🌐 PASUL 2: Import Foneday")
            total_foneday = step2_import_foneday_all_products()
            st.success(f"✅ Complet: {total_foneday} produse")
    
    with col3:
        st.markdown("### Pasul 3")
        st.caption("🗺️ Mapare SKU")
        if st.button("▶️ Rulează", key="btn_step3", use_container_width=True):
            st.markdown("## 🗺️ PASUL 3: Mapare")
            total_mapped = step3_map_sku_to_artcode()
            st.success(f"✅ Complet: {total_mapped} mapări")
    
    with col4:
        st.markdown("### Pasul 4")
        st.caption("🔍 Verificare Stoc")
        if st.button("▶️ Rulează", key="btn_step4", use_container_width=True):
            st.markdown("## 🔍 PASUL 4: Verificare")
            checked, available = step4_check_stock_and_prices()
            st.success(f"✅ Complet: {available} disponibile")
    
    with col5:
        st.markdown("### Pasul 5")
        st.caption("🛒 Adăugare Coș")
        if st.button("▶️ Rulează", key="btn_step5", use_container_width=True):
            st.markdown("## 🛒 PASUL 5: Coș")
            added, not_profitable = step5_add_to_cart()
            st.success(f"✅ Complet: {added} adăugate")
    
    st.markdown("---")
    
    st.markdown("### Sau alege mai mulți pași:")
    
    col1, col2, col3, col4, col5 = st.columns(5)
    
    with col1:
        run_step1 = st.checkbox("Pasul 1", value=False)
    with col2:
        run_step2 = st.checkbox("Pasul 2", value=False)
    with col3:
        run_step3 = st.checkbox("Pasul 3", value=False)
    with col4:
        run_step4 = st.checkbox("Pasul 4", value=False)
    with col5:
        run_step5 = st.checkbox("Pasul 5", value=False)
    
    if st.button("▶️ RULEAZĂ PAȘII SELECTAȚI", type="primary", use_container_width=True):
        
        start_time = datetime.now()
        
        if run_step1:
            st.markdown("## 📥 PASUL 1: Import WooCommerce")
            new, updated, unchanged, errors = step1_import_woocommerce()
            st.success(f"✅ PASUL 1: {new} noi, {updated} actualizate")
            st.markdown("---")
        
        if run_step2:
            st.markdown("## 🌐 PASUL 2: Import Foneday")
            total_foneday = step2_import_foneday_all_products()
            st.success(f"✅ PASUL 2: {total_foneday} produse")
            st.markdown("---")
        
        if run_step3:
            st.markdown("## 🗺️ PASUL 3: Mapare")
            total_mapped = step3_map_sku_to_artcode()
            st.success(f"✅ PASUL 3: {total_mapped} mapări")
            st.markdown("---")
        
        if run_step4:
            st.markdown("## 🔍 PASUL 4: Verificare")
            checked, available = step4_check_stock_and_prices()
            st.success(f"✅ PASUL 4: {available} disponibile")
            st.markdown("---")
        
        if run_step5:
            st.markdown("## 🛒 PASUL 5: Coș")
            added, not_profitable = step5_add_to_cart()
            st.success(f"✅ PASUL 5: {added} adăugate")
        
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()
        
        st.markdown("---")
        st.success(f"🎉 **Finalizat în {duration:.0f}s ({duration/60:.1f} min)!**")


elif page == "💰 Oportunități Profit":
    st.title("💰 Căutare Oportunități de Profit Mare")
    
    st.markdown("""
    ### Descoperă oportunități de profit excepționale!
    
    Această funcție caută în catalogul tău produse care au marje de profit foarte mari la Foneday, 
    **DOAR pentru produsele cu stoc ≥ 1**.
    
    🎯 **Beneficii:**
    - Descoperi produse profitabile pe care le-ai putea vinde mai mult
    - Găsești oportunități de arbitraj (cumperi ieftin, vinzi scump)
    - **EXCLUDE produsele cu stoc 0** (acestea sunt procesate automat în Pașii 4-5)
    
    ⚠️ **Notă**: 
    - Procesul poate dura 5-10 minute pentru catalog mare
    - **Produsele cu stoc 0** nu apar aici - ele sunt procesate separat în workflow-ul standard (Pașii 4-5)
    - Această funcție e pentru **reaprovizionare strategică** a produselor profitabile care deja se vând
    """)
    
    st.markdown("---")
    
    min_profit = st.slider(
        "Setează marja minimă de profit (%)",
        min_value=15,
        max_value=100,
        value=30,
        step=5,
        help="Caută produse cu profit mai mare decât acest procent"
    )
    
    st.info(f"🎯 Caut produse cu stoc ≥1 și profit ≥ **{min_profit}%**")
    
    st.markdown("---")
    
    if st.button("🔍 CAUTĂ OPORTUNITĂȚI", type="primary", use_container_width=True):
        
        opportunities = find_high_profit_opportunities(min_profit)
        
        if opportunities:
            st.success(f"🎉 Găsite {len(opportunities)} oportunități de profit ≥{min_profit}% (produse cu stoc)!")
            
            st.session_state['opportunities'] = opportunities
            
            df = pd.DataFrame(opportunities)
            df = df.sort_values("profit_margin", ascending=False)
            
            st.dataframe(
                df[[
                    "sku", "product_name", "woo_price_ron", "foneday_price_eur",
                    "profit_margin", "current_stock", "quality"
                ]],
                use_container_width=True,
                height=400
            )
            
            st.markdown("---")
            col1, col2, col3, col4 = st.columns(4)
            
            with col1:
                st.metric("💰 Total Oportunități", len(opportunities))
            
            with col2:
                avg_profit = df["profit_margin"].mean()
                st.metric("📈 Profit Mediu", f"{avg_profit:.1f}%")
            
            with col3:
                max_profit = df["profit_margin"].max()
                st.metric("🏆 Profit Maxim", f"{max_profit:.1f}%")
            
            with col4:
                total_stock = df["current_stock"].sum()
                st.metric("📦 Total Stoc Actual", int(total_stock))
        else:
            st.warning(f"Nu s-au găsit oportunități cu stoc ≥1 și profit ≥{min_profit}%")
            st.info("💡 Sugestii:\n- Încearcă o marjă mai mică\n- Asigură-te că ai rulat PASUL 2 (Import Foneday) și PASUL 3 (Mapare)\n- Produsele cu stoc 0 sunt procesate în Pașii 4-5")
    
    if 'opportunities' in st.session_state and st.session_state['opportunities']:
        st.markdown("---")
        st.markdown("## 🛒 Comandă Produse Selectate")
        
        st.info("💡 Completează cantitatea dorită pentru fiecare produs. Produsele cu cantitate 0 sau goală nu vor fi comandate.")
        
        opportunities = st.session_state['opportunities']
        
        quantities = {}
        
        col1, col2, col3, col4, col5, col6 = st.columns([2, 2, 1, 1, 1, 1])
        with col1:
            st.markdown("**SKU**")
        with col2:
            st.markdown("**Produs**")
        with col3:
            st.markdown("**Profit %**")
        with col4:
            st.markdown("**Stoc Actual**")
        with col5:
            st.markdown("**Preț EUR**")
        with col6:
            st.markdown("**Cantitate**")
        
        st.markdown("---")
        
        for idx, opp in enumerate(opportunities):
            col1, col2, col3, col4, col5, col6 = st.columns([2, 2, 1, 1, 1, 1])
            
            with col1:
                st.text(opp["sku"])
            with col2:
                st.text(opp["product_name"][:30] + "..." if len(opp["product_name"]) > 30 else opp["product_name"])
            with col3:
                st.text(f"{opp['profit_margin']:.1f}%")
            with col4:
                st.text(str(opp["current_stock"]))
            with col5:
                st.text(f"€{opp['foneday_price_eur']:.2f}")
            with col6:
                qty = st.number_input(
                    "Qty",
                    min_value=0,
                    max_value=100,
                    value=0,
                    step=1,
                    key=f"qty_{idx}",
                    label_visibility="collapsed"
                )
                quantities[idx] = qty
        
        st.markdown("---")
        
        col1, col2, col3 = st.columns([1, 1, 1])
        
        with col2:
            if st.button("🛒 PLASEAZĂ COMANDA", type="primary", use_container_width=True):
                
                to_order = []
                for idx, qty in quantities.items():
                    if qty > 0:
                        to_order.append({
                            "opportunity": opportunities[idx],
                            "quantity": qty
                        })
                
                if not to_order:
                    st.warning("⚠️ Nu ai selectat nicio cantitate! Completează cantitățile mai întâi.")
                else:
                    st.info(f"📦 Plasez comandă pentru {len(to_order)} produse...")
                    
                    success_count = 0
                    error_count = 0
                    
                    progress_bar_order = st.progress(0)
                    status_order = st.empty()
                    
                    for idx, item in enumerate(to_order):
                        opp = item["opportunity"]
                        qty = item["quantity"]
                        
                        status_order.info(f"🛒 Comand {idx+1}/{len(to_order)}: {opp['sku']} × {qty}")
                        progress_bar_order.progress((idx + 1) / len(to_order))
                        
                        cart_result = add_to_foneday_cart(opp["foneday_sku"], qty, f"Oportunitate profit {opp['profit_margin']:.1f}% - {opp['sku']}")
                        
                        if cart_result:
                            try:
                                supabase.table("claude_foneday_cart").insert({
                                    "product_id": None,
                                    "sku": opp["sku"],
                                    "foneday_sku": opp["foneday_sku"],
                                    "quantity": qty,
                                    "price_eur": opp["foneday_price_eur"],
                                    "woo_price_ron": opp["woo_price_ron"],
                                    "profit_margin": opp["profit_margin"],
                                    "is_profitable": True,
                                    "status": "added_to_cart",
                                    "note": f"Oportunitate - Profit: {opp['profit_margin']:.1f}% - {qty} buc"
                                }).execute()
                                
                                success_count += 1
                                log_event("opportunity_order", f"Comandat: {opp['sku']} × {qty} - Profit: {opp['profit_margin']:.1f}%", sku=opp['sku'], status="success")
                            except Exception as e:
                                error_count += 1
                        else:
                            error_count += 1
                        
                        time.sleep(0.2)
                    
                    progress_bar_order.progress(1.0)
                    status_order.empty()
                    
                    st.success(f"✅ Comanda finalizată! {success_count} produse adăugate în coș, {error_count} erori.")
                    
                    if success_count > 0:
                        total_value = sum([item["opportunity"]["foneday_price_eur"] * item["quantity"] for item in to_order])
                        st.info(f"💰 Valoare totală comandă: €{total_value:.2f}")
        
        st.markdown("---")
        
        if st.button("📥 Exportă Lista (CSV)"):
            df = pd.DataFrame(opportunities)
            csv = df.to_csv(index=False)
            st.download_button(
                label="⬇️ Descarcă CSV",
                data=csv,
                file_name=f"oportunitati_profit_{min_profit}pct_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv"
            )


elif page == "📊 Stocuri Critice":
    st.title("⚠️ Produse cu Stoc Zero")
    
    try:
        critical = supabase.table("claude_v_critical_stock").select("*").execute()
        
        if critical.data and len(critical.data) > 0:
            df = pd.DataFrame(critical.data)
            
            st.metric("📊 Total Produse Stoc Zero", len(df))
            
            st.dataframe(
                df[[
                    "sku", "name", "stock_quantity", "woo_price_ron",
                    "foneday_sku", "foneday_price_eur", "foneday_instock",
                    "profit_margin_percent"
                ]],
                use_container_width=True,
                height=500
            )
        else:
            st.success("✅ Nu există produse cu stoc zero!")
    except Exception as e:
        st.error(f"Eroare: {e}")


elif page == "🛒 Coș Foneday":
    st.title("🛒 Produse în Coșul Foneday")
    
    try:
        cart = supabase.table("claude_foneday_cart").select("*").order("created_at", desc=True).limit(200).execute()
        
        if cart.data and len(cart.data) > 0:
            df = pd.DataFrame(cart.data)
            
            st.dataframe(
                df[[
                    "created_at", "sku", "foneday_sku",
                    "quantity", "price_eur", "woo_price_ron",
                    "profit_margin", "is_profitable", "status", "note"
                ]],
                use_container_width=True,
                height=500
            )
            
            st.markdown("---")
            col1, col2, col3 = st.columns(3)
            
            with col1:
                total_value = (df["price_eur"] * df["quantity"]).sum()
                st.metric("💰 Valoare Totală (EUR)", f"€{total_value:.2f}")
            
            with col2:
                profitable_df = df[df["is_profitable"] == True]
                if len(profitable_df) > 0:
                    avg_margin = profitable_df["profit_margin"].mean()
                    st.metric("📈 Marjă Medie", f"{avg_margin:.2f}%")
                else:
                    st.metric("📈 Marjă Medie", "N/A")
            
            with col3:
                total_items = df["quantity"].sum()
                st.metric("📦 Total Bucăți", int(total_items))
        else:
            st.info("Nu există produse în coș")
    except Exception as e:
        st.error(f"Eroare: {e}")


elif page == "🚚 Comenzi în Tranzit":
    st.title("🚚 Comenzi în Tranzit (Pending)")
    
    st.markdown("""
    ### Gestionează comenzi plasate la Foneday care sunt în livrare
    
    📦 **Comenzile confirmate** sunt excluse automat din Pașii 4-5 pentru a evita comenzi duplicate.
    
    **Flow:**
    1. Plasezi comanda la Foneday
    2. Confirmi comanda aici → status **pending**
    3. La livrare → marchezi ca **delivered** sau **ștergi**
    """)
    
    st.markdown("---")
    
    tab1, tab2, tab3 = st.tabs(["🛒 Coș de Confirmat", "🚚 În Tranzit", "📦 Istoric"])
    
    with tab1:
        st.markdown("## 🛒 Produse din Coș - De Confirmat")
        
        cart = supabase.table("claude_foneday_cart").select("*").eq("status", "added_to_cart").order("created_at", desc=True).execute()
        
        if cart.data and len(cart.data) > 0:
            st.info(f"📊 Găsite {len(cart.data)} produse în coș de confirmat")
            
            for idx, item in enumerate(cart.data):
                col1, col2, col3, col4 = st.columns([3, 1, 1, 2])
                
                with col1:
                    st.text(f"{item['sku']} - {item['quantity']} buc - €{item['price_eur']:.2f}")
                
                with col2:
                    st.text(f"{item['profit_margin']:.1f}%")
                
                with col3:
                    expected_delivery = st.date_input(
                        "Livrare",
                        value=datetime.now() + timedelta(days=4),
                        key=f"delivery_{idx}",
                        label_visibility="collapsed"
                    )
                
                with col4:
                    if st.button("✅ Confirmă Comandă", key=f"confirm_{idx}", use_container_width=True):
                        try:
                            supabase.table("claude_foneday_orders_pending").insert({
                                "sku": item["sku"],
                                "foneday_sku": item["foneday_sku"],
                                "quantity": item["quantity"],
                                "order_date": item["created_at"],
                                "expected_delivery_date": expected_delivery.isoformat(),
                                "confirmed_at": datetime.now().isoformat(),
                                "confirmed_by": "manual",
                                "status": "pending",
                                "note": item.get("note", "")
                            }).execute()
                            
                            supabase.table("claude_foneday_cart").update({
                                "status": "confirmed"
                            }).eq("id", item["id"]).execute()
                            
                            st.success(f"✅ Comanda confirmată: {item['sku']} × {item['quantity']}")
                            time.sleep(1)
                            st.rerun()
                        except Exception as e:
                            st.error(f"Eroare: {e}")
        else:
            st.info("Coșul e gol - nu există comenzi de confirmat")
    
    with tab2:
        st.markdown("## 🚚 Comenzi în Livrare")
        
        pending = supabase.table("claude_foneday_orders_pending").select("*").eq("status", "pending").order("expected_delivery_date").execute()
        
        if pending.data and len(pending.data) > 0:
            st.info(f"📦 Găsite {len(pending.data)} comenzi în tranzit")
            
            for idx, order in enumerate(pending.data):
                col1, col2, col3, col4, col5 = st.columns([2, 1, 1, 1, 2])
                
                with col1:
                    st.text(f"{order['sku']} × {order['quantity']}")
                
                with col2:
                    order_date = datetime.fromisoformat(order['order_date']).strftime('%Y-%m-%d')
                    st.text(f"📅 {order_date}")
                
                with col3:
                    if order['expected_delivery_date']:
                        st.text(f"🚚 {order['expected_delivery_date']}")
                    else:
                        st.text("🚚 N/A")
                
                with col4:
                    days_ago = (datetime.now() - datetime.fromisoformat(order['order_date'])).days
                    st.text(f"{days_ago}d")
                
                with col5:
                    col_delivered, col_cancel = st.columns(2)
                    
                    with col_delivered:
                        if st.button("✅ Livrat", key=f"deliver_{idx}", use_container_width=True):
                            try:
                                supabase.table("claude_foneday_orders_pending").update({
                                    "status": "delivered",
                                    "updated_at": datetime.now().isoformat()
                                }).eq("id", order["id"]).execute()
                                
                                st.success(f"✅ Marcat ca livrat: {order['sku']}")
                                time.sleep(1)
                                st.rerun()
                            except Exception as e:
                                st.error(f"Eroare: {e}")
                    
                    with col_cancel:
                        if st.button("❌ Anulează", key=f"cancel_{idx}", use_container_width=True):
                            try:
                                supabase.table("claude_foneday_orders_pending").update({
                                    "status": "cancelled",
                                    "updated_at": datetime.now().isoformat()
                                }).eq("id", order["id"]).execute()
                                
                                st.warning(f"❌ Comandă anulată: {order['sku']}")
                                time.sleep(1)
                                st.rerun()
                            except Exception as e:
                                st.error(f"Eroare: {e}")
        else:
            st.success("✅ Nu există comenzi în tranzit")
    
    with tab3:
        st.markdown("## 📦 Istoric Comenzi")
        
        history = supabase.table("claude_foneday_orders_pending").select("*").in_("status", ["delivered", "cancelled"]).order("updated_at", desc=True).limit(50).execute()
        
        if history.data:
            df = pd.DataFrame(history.data)
            st.dataframe(
                df[["sku", "quantity", "order_date", "status", "expected_delivery_date", "updated_at"]],
                use_container_width=True,
                height=400
            )
        else:
            st.info("Nu există istoric")


elif page == "🗺️ Mapări":
    st.title("🗺️ Mapări SKU → artcode")
    
    try:
        mappings = supabase.table("claude_sku_artcode_mapping").select("*").order("created_at", desc=True).limit(500).execute()
        
        if mappings.data and len(mappings.data) > 0:
            df = pd.DataFrame(mappings.data)
            
            st.metric("🗺️ Total Mapări", len(df))
            
            st.dataframe(
                df[["my_sku", "foneday_artcode", "foneday_sku", "mapping_score", "last_verified_at"]],
                use_container_width=True,
                height=500
            )
        else:
            st.info("Nu există mapări. Rulează PASUL 3.")
    except Exception as e:
        st.error(f"Eroare: {e}")


elif page == "📝 Log":
    st.title("📝 Istoric Log")
    
    try:
        logs = supabase.table("claude_sync_logs").select("*").order("created_at", desc=True).limit(200).execute()
        
        if logs.data:
            df = pd.DataFrame(logs.data)
            df["created_at"] = pd.to_datetime(df["created_at"]).dt.strftime("%Y-%m-%d %H:%M:%S")
            st.dataframe(
                df[["created_at", "event_type", "sku", "message", "status"]],
                use_container_width=True,
                height=500
            )
        else:
            st.info("Nu există log-uri")
    except Exception as e:
        st.error(f"Eroare: {e}")


st.sidebar.markdown("---")
st.sidebar.caption("📦 ServicePack v4.0")
st.sidebar.caption("Tracking comenzi în tranzit")
