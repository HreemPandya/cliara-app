-- Cliara Cloud — initial schema
-- Run this in the Supabase SQL editor (Dashboard > SQL Editor > New query)

-- Per-user monthly query counts.
-- The primary key (user_id, year_month) makes upserts idempotent.
CREATE TABLE IF NOT EXISTS public.user_usage (
    user_id    UUID   NOT NULL,
    year_month TEXT   NOT NULL CHECK (year_month ~ '^\d{4}-\d{2}$'),
    query_count INT   NOT NULL DEFAULT 0 CHECK (query_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, year_month)
);

-- Keep updated_at fresh automatically.
CREATE OR REPLACE FUNCTION public.set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_user_usage_updated_at ON public.user_usage;
CREATE TRIGGER trg_user_usage_updated_at
    BEFORE UPDATE ON public.user_usage
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- Row Level Security: this table is only accessed by the backend
-- using the service role key, which bypasses RLS.
-- We still enable RLS so anonymous/authenticated users cannot read
-- or write usage data directly via the PostgREST API.
ALTER TABLE public.user_usage ENABLE ROW LEVEL SECURITY;

-- No policies = nobody (other than service role) can access rows.
-- Add a policy here if you ever want users to read their own usage:
--
-- CREATE POLICY "Users can view own usage"
--   ON public.user_usage FOR SELECT
--   USING (auth.uid() = user_id);
