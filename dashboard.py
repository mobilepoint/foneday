-- Creează funcția pentru updated_at în schema public (dacă nu există)
CREATE OR REPLACE FUNCTION public.update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Tabel pentru toate produsele din Foneday
CREATE TABLE IF NOT EXISTS public.claude_foneday_products (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    foneday_sku TEXT NOT NULL UNIQUE,
    artcode TEXT,
    ean TEXT,
    title TEXT,
    instock TEXT,
    suitable_for TEXT,
    category TEXT,
    product_brand TEXT,
    quality TEXT,
    model_brand TEXT,
    model_codes JSONB,
    price_eur DECIMAL(10, 2),
    last_sync_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_claude_foneday_products_sku ON public.claude_foneday_products(foneday_sku);
CREATE INDEX IF NOT EXISTS idx_claude_foneday_products_artcode ON public.claude_foneday_products(artcode);
CREATE INDEX IF NOT EXISTS idx_claude_foneday_products_instock ON public.claude_foneday_products(instock);

-- Trigger pentru updated_at
DROP TRIGGER IF EXISTS update_claude_foneday_products_updated_at ON public.claude_foneday_products;
CREATE TRIGGER update_claude_foneday_products_updated_at 
BEFORE UPDATE ON public.claude_foneday_products 
FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();

-- Tabel pentru maparea SKU (al tău) -> artcode (Foneday)
CREATE TABLE IF NOT EXISTS public.claude_sku_artcode_mapping (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    my_sku TEXT NOT NULL,
    foneday_artcode TEXT NOT NULL,
    foneday_sku TEXT,
    product_id UUID,
    mapping_score INTEGER DEFAULT 100,
    last_verified_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(my_sku, foneday_artcode)
);

CREATE INDEX IF NOT EXISTS idx_claude_sku_artcode_my_sku ON public.claude_sku_artcode_mapping(my_sku);
CREATE INDEX IF NOT EXISTS idx_claude_sku_artcode_foneday ON public.claude_sku_artcode_mapping(foneday_artcode);
