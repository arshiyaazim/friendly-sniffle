-- 003_fpe_search_indexes.sql
-- Indexes to support fast read-only employee search & transaction history.
-- Strictly additive. No schema/data mutation.

CREATE INDEX IF NOT EXISTS idx_fpe_emp_id_phone
    ON fpe_employees(employee_id_phone)
    WHERE employee_id_phone IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_fpe_emp_primary_phone
    ON fpe_employees(primary_phone)
    WHERE primary_phone IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_fpe_emp_name_norm
    ON fpe_employees(name_normalized);

CREATE INDEX IF NOT EXISTS idx_fpe_emp_canonical
    ON fpe_employees(canonical_employee_id)
    WHERE canonical_employee_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_fpe_txn_date_desc
    ON fpe_cash_transactions(txn_date DESC, id DESC);
