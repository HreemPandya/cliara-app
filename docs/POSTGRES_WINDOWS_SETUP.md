# PostgreSQL Setup Guide for Windows

## Option 1: Install PostgreSQL Locally (Recommended for Development)

### Step 1: Download PostgreSQL

1. Go to: https://www.postgresql.org/download/windows/
2. Click "Download the installer"
3. Download **PostgreSQL 15** or **16** (latest stable)
4. Run the installer

### Step 2: Install PostgreSQL

During installation:

1. **Installation Directory**: Keep default (`C:\Program Files\PostgreSQL\15`)
2. **Select Components**: 
   - ✅ PostgreSQL Server
   - ✅ pgAdmin 4 (GUI tool - recommended)
   - ✅ Command Line Tools
   - ✅ Stack Builder (optional)
3. **Data Directory**: Keep default
4. **Password**: **Remember this password!** You'll need it for the `postgres` superuser
5. **Port**: Keep default `5432`
6. **Locale**: Keep default

### Step 3: Verify Installation

Open **PowerShell** or **Command Prompt** and run:

```powershell
# Check if PostgreSQL service is running
Get-Service -Name postgresql*

# Or check if psql is available
psql --version
```

If `psql` is not found, add PostgreSQL to PATH:
- PostgreSQL bin directory: `C:\Program Files\PostgreSQL\15\bin`
- Add to System PATH environment variable

### Step 4: Start PostgreSQL Service

```powershell
# Start PostgreSQL service
Start-Service postgresql-x64-15

# Or use Services GUI:
# Win+R → services.msc → Find "postgresql-x64-15" → Start
```

### Step 5: Create Database for Cliara

Open **Command Prompt** or **PowerShell**:

```powershell
# Connect to PostgreSQL (use the password you set during installation)
psql -U postgres

# Or if psql is not in PATH:
& "C:\Program Files\PostgreSQL\15\bin\psql.exe" -U postgres
```

In the PostgreSQL prompt:

```sql
-- Create database
CREATE DATABASE cliara;

-- Create user
CREATE USER cliara WITH PASSWORD 'your_secure_password_here';

-- Grant privileges
GRANT ALL PRIVILEGES ON DATABASE cliara TO cliara;

-- Connect to cliara database
\c cliara

-- Grant schema privileges
GRANT ALL ON SCHEMA public TO cliara;

-- Exit
\q
```

### Step 6: Test Connection

```powershell
# Test connection
psql -U cliara -d cliara -h localhost
# Enter password when prompted
```

---

## Option 2: Use Docker (Easier, No Installation)

If you have Docker installed:

```powershell
# Run PostgreSQL in Docker
docker run --name cliara-postgres `
  -e POSTGRES_USER=cliara `
  -e POSTGRES_PASSWORD=your_password `
  -e POSTGRES_DB=cliara `
  -p 5432:5432 `
  -d postgres:15

# Check if running
docker ps

# View logs
docker logs cliara-postgres
```

**Connection string:**
```
postgresql://cliara:your_password@localhost:5432/cliara
```

---

## Option 3: Use Cloud PostgreSQL (Free Tier)

### Supabase (Recommended - Easiest)

1. Go to: https://supabase.com
2. Sign up (free)
3. Create new project
4. Go to **Settings → Database**
5. Copy **Connection string** (URI format)
6. Use that connection string in Cliara config

**Example connection string:**
```
postgresql://postgres:[YOUR-PASSWORD]@db.xxxxx.supabase.co:5432/postgres
```

### Railway

1. Go to: https://railway.app
2. Sign up (free $5 credit/month)
3. New Project → Add PostgreSQL
4. Copy connection string

### Neon

1. Go to: https://neon.tech
2. Sign up (free tier)
3. Create project
4. Copy connection string

---

## Configure Cliara to Use PostgreSQL

### Step 1: Install psycopg2

```powershell
pip install psycopg2-binary
```

### Step 2: Set Environment Variable

Create or edit `.env` file in project root:

```env
POSTGRES_PASSWORD=your_secure_password_here
```

### Step 3: Update Cliara Config

Edit `~/.cliara/config.json` (or create it):

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

**OR** use connection string:

```json
{
  "storage_backend": "postgres",
  "connection_string": "postgresql://cliara:your_password@localhost:5432/cliara"
}
```

### Step 4: Test Cliara with PostgreSQL

```powershell
cliara
```

Try creating a macro:
```
cliara ❯ macro add test
```

---

## Quick Setup Script (PowerShell)

Save this as `setup-postgres.ps1`:

```powershell
# Setup PostgreSQL for Cliara

Write-Host "Setting up PostgreSQL for Cliara..." -ForegroundColor Green

# Check if PostgreSQL is installed
$pgPath = "C:\Program Files\PostgreSQL\15\bin\psql.exe"
if (-not (Test-Path $pgPath)) {
    Write-Host "PostgreSQL not found. Please install PostgreSQL first." -ForegroundColor Red
    Write-Host "Download from: https://www.postgresql.org/download/windows/" -ForegroundColor Yellow
    exit 1
}

# Get password
$password = Read-Host "Enter PostgreSQL password for 'postgres' user" -AsSecureString
$passwordPlain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($password)
)

# Create database and user
Write-Host "Creating database and user..." -ForegroundColor Yellow

$env:PGPASSWORD = $passwordPlain
& $pgPath -U postgres -c "CREATE DATABASE cliara;" 2>$null
& $pgPath -U postgres -c "CREATE USER cliara WITH PASSWORD 'your_secure_password';" 2>$null
& $pgPath -U postgres -c "GRANT ALL PRIVILEGES ON DATABASE cliara TO cliara;" 2>$null
& $pgPath -U postgres -d cliara -c "GRANT ALL ON SCHEMA public TO cliara;" 2>$null

Write-Host "✓ Database created!" -ForegroundColor Green

# Create config
$configDir = "$env:USERPROFILE\.cliara"
if (-not (Test-Path $configDir)) {
    New-Item -ItemType Directory -Path $configDir | Out-Null
}

$config = @{
    storage_backend = "postgres"
    postgres = @{
        host = "localhost"
        port = 5432
        database = "cliara"
        user = "cliara"
    }
} | ConvertTo-Json -Depth 3

$config | Out-File "$configDir\config.json" -Encoding UTF8

Write-Host "✓ Config created at $configDir\config.json" -ForegroundColor Green

# Set environment variable
Write-Host "`nAdd this to your .env file:" -ForegroundColor Yellow
Write-Host "POSTGRES_PASSWORD=your_secure_password" -ForegroundColor Cyan

Write-Host "`n✓ Setup complete!" -ForegroundColor Green
```

---

## Troubleshooting

### PostgreSQL Service Not Running

```powershell
# Check service status
Get-Service postgresql*

# Start service
Start-Service postgresql-x64-15

# Or use Services GUI
services.msc
```

### Connection Refused

1. Check if PostgreSQL is running: `Get-Service postgresql*`
2. Check firewall: Allow port 5432
3. Verify connection: `psql -U cliara -d cliara -h localhost`

### Authentication Failed

1. Check password in `.env` file
2. Verify user exists: `psql -U postgres -c "\du"`
3. Reset password: `psql -U postgres -c "ALTER USER cliara WITH PASSWORD 'new_password';"`

### psql Not Found

Add to PATH:
1. Win+R → `sysdm.cpl` → Advanced → Environment Variables
2. Edit "Path" → Add: `C:\Program Files\PostgreSQL\15\bin`
3. Restart terminal

---

## Using pgAdmin (GUI Tool)

If you installed pgAdmin 4:

1. Open **pgAdmin 4** from Start Menu
2. Connect to server (use `postgres` user password)
3. Right-click **Databases** → **Create** → **Database**
   - Name: `cliara`
4. Right-click **Login/Group Roles** → **Create** → **Login/Group Role**
   - Name: `cliara`
   - Password: `your_password`
   - Privileges: ✅ Can login
5. Right-click `cliara` database → **Properties** → **Security**
   - Add user `cliara` with all privileges

---

## Next Steps

1. ✅ PostgreSQL installed and running
2. ✅ Database `cliara` created
3. ✅ User `cliara` created
4. ✅ Cliara config updated
5. ✅ Test with: `cliara`

**You're ready to use PostgreSQL with Cliara!** 🚀
