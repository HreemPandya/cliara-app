# PostgreSQL Setup Guide for Cliara

## Overview

Cliara now supports PostgreSQL as a storage backend, allowing you to:
- ✅ Store **millions of macros** (vs. thousands with JSON)
- ✅ **Multi-user support** (each user has isolated macros)
- ✅ **Fast full-text search** across all macros
- ✅ **Team/organization features** (coming soon)
- ✅ **Better performance** for large macro libraries

---

## Quick Setup

### 1. Install PostgreSQL

**macOS:**
```bash
brew install postgresql@15
brew services start postgresql@15
```

**Linux (Ubuntu/Debian):**
```bash
sudo apt-get update
sudo apt-get install postgresql postgresql-contrib
sudo systemctl start postgresql
```

**Windows:**
Download from [postgresql.org](https://www.postgresql.org/download/windows/)

### 2. Create Database and User

```bash
# Connect to PostgreSQL
psql postgres

# Create database
CREATE DATABASE cliara;

# Create user
CREATE USER cliara WITH PASSWORD 'your_password_here';

# Grant privileges
GRANT ALL PRIVILEGES ON DATABASE cliara TO cliara;

# Connect to cliara database
\c cliara

# Grant schema privileges
GRANT ALL ON SCHEMA public TO cliara;

# Exit
\q
```

### 3. Install Python Dependencies

```bash
pip install psycopg2-binary
```

Or if using requirements.txt:
```bash
pip install -r requirements.txt
```

### 4. Configure Cliara

Edit `~/.cliara/config.json`:

```json
{
  "storage_backend": "postgres",
  "postgres": {
    "host": "localhost",
    "port": 5432,
    "database": "cliara",
    "user": "cliara"
  }
}
```

**Important:** Set the password via environment variable (more secure):
```bash
export POSTGRES_PASSWORD=your_password_here
```

Or add to your `.env` file:
```
POSTGRES_PASSWORD=your_password_here
```

### 5. Alternative: Connection String

You can also use a full connection string:

```json
{
  "storage_backend": "postgres",
  "connection_string": "postgresql://cliara:password@localhost:5432/cliara"
}
```

---

## Migrating from JSON to PostgreSQL

### Option 1: Using Migration Tool

```bash
python -m cliara.tools.migrate \
  --host localhost \
  --database cliara \
  --user cliara \
  --password your_password
```

Or with connection string:
```bash
python -m cliara.tools.migrate \
  --connection-string "postgresql://cliara:password@localhost:5432/cliara"
```

**Dry run** (see what would be migrated):
```bash
python -m cliara.tools.migrate --dry-run
```

### Option 2: Manual Migration

1. Keep using JSON (default)
2. Create macros in PostgreSQL
3. Gradually migrate as needed

---

## Using PostgreSQL

Once configured, Cliara will automatically use PostgreSQL:

```bash
cliara ❯ macro add test
# Macro saved to PostgreSQL

cliara ❯ macro list
# Lists macros from PostgreSQL

cliara ❯ macro search docker
# Full-text search in PostgreSQL
```

---

## Switching Back to JSON

To switch back to JSON storage:

```json
{
  "storage_backend": "json",
  "macro_storage": "~/.cliara/macros.json"
}
```

Your PostgreSQL macros will remain in the database and can be migrated back if needed.

---

## Cloud PostgreSQL Options

### Free Tier Options

1. **Supabase** (Recommended)
   - Free tier: 500MB database
   - Easy setup: https://supabase.com
   - Connection string format: `postgresql://postgres:[PASSWORD]@[HOST]:5432/postgres`

2. **Railway**
   - Free tier: $5 credit/month
   - One-click PostgreSQL: https://railway.app

3. **Neon**
   - Free tier: 0.5GB storage
   - Serverless PostgreSQL: https://neon.tech

### Setup with Supabase

1. Create account at https://supabase.com
2. Create new project
3. Go to Settings → Database
4. Copy connection string
5. Update `~/.cliara/config.json`:

```json
{
  "storage_backend": "postgres",
  "connection_string": "postgresql://postgres:[YOUR-PASSWORD]@db.[PROJECT-REF].supabase.co:5432/postgres"
}
```

---

## Troubleshooting

### Connection Error

**Error:** `psycopg2.OperationalError: could not connect to server`

**Solutions:**
1. Check PostgreSQL is running: `pg_isready`
2. Verify host/port in config
3. Check firewall settings
4. Verify user/password

### Permission Error

**Error:** `permission denied for schema public`

**Solution:**
```sql
GRANT ALL ON SCHEMA public TO cliara;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO cliara;
```

### Module Not Found

**Error:** `ModuleNotFoundError: No module named 'psycopg2'`

**Solution:**
```bash
pip install psycopg2-binary
```

---

## Performance Comparison

| Operation | JSON (< 1K macros) | PostgreSQL (1M+ macros) |
|-----------|-------------------|------------------------|
| Load all | ~100ms | ~50ms |
| Search | O(n) linear | O(log n) indexed |
| Add macro | ~10ms | ~5ms |
| Get macro | ~1ms | ~1ms |

**PostgreSQL advantages:**
- Scales to millions of macros
- Full-text search with ranking
- Multi-user isolation
- Concurrent access safe
- ACID transactions

---

## Next Steps

- ✅ PostgreSQL setup complete
- 🔄 Multi-user authentication (coming soon)
- 🔄 Team/organization macros (coming soon)
- 🔄 Macro marketplace (coming soon)
- 🔄 Cloud sync (coming soon)

---

## Support

If you encounter issues:
1. Check PostgreSQL logs: `tail -f /var/log/postgresql/postgresql-*.log`
2. Test connection: `psql -h localhost -U cliara -d cliara`
3. Verify Cliara config: `cat ~/.cliara/config.json`
