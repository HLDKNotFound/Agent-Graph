# Database Infrastructure & Prisma Setup Guide

## 1. Executive Summary & Architecture Overview

This document details the PostgreSQL 16+ and Prisma ORM database infrastructure established for the **Enterprise Knowledge Agent**.

- **Database Engine**: PostgreSQL 16.10 with `pgvector` (v0.7.4) extension for dense vector embeddings and HNSW indexing.
- **ORM & Schema Authority**: Prisma ORM (v5.22.0 stable) with canonical schema path at [`backend/schema.prisma`](file:///home/mercury/AgentGraph/backend/schema.prisma).
- **Environment & Tenancy**: Dynamic, zero-hardcoding environment configuration via [`.env`](file:///home/mercury/AgentGraph/.env) and template [`.env.example`](file:///home/mercury/AgentGraph/.env.example).
- **Dual Runtime Support**:
  1. **Native Runtime**: PostgreSQL 16.10 running locally via `conda activate ml-env` with `pg_ctl`.
  2. **Containerized Runtime**: Docker Compose stack (`pgvector/pgvector:pg16`) via [`docker-compose.yml`](file:///home/mercury/AgentGraph/docker-compose.yml).

---

## 2. Root Cause Analysis & Resolution: `npm install` Hang

### Root Causes
1. **npm 11 `allow-scripts` Mechanism**: npm v11.16.0 introduced interactive lifecycle script validation (`allow-scripts`), which blocked or paused non-interactive terminal execution during Prisma preinstall and postinstall binary downloads (`@prisma/engines`).
2. **Prisma 8 RC Deprecation Incompatibility**: Invoking bare `npx prisma` fetched `prisma@8.0.0-rc.34` (the latest unpinned release tag), where `prisma validate` has been deprecated and removed in favor of `contract emit`.
3. **WSL2 DNS / Fetch Timeout Latency**: In WSL2 Linux environments, default indefinite network fetch retries can hang when downloading engine binaries from external CDNs.

### Permanent Resolutions Applied
1. **Configured NPM Runtime Parameters**: Created [`.npmrc`](file:///home/mercury/AgentGraph/.npmrc) and [`backend/.npmrc`](file:///home/mercury/AgentGraph/backend/.npmrc) with:
   ```ini
   audit=false
   fund=false
   fetch-retries=3
   fetch-retry-mintimeout=5000
   fetch-retry-maxtimeout=30000
   fetch-timeout=30000
   engine-strict=false
   ```
2. **Pinned Prisma 5.22.0 in `backend/package.json`**: Explicitly pinned stable `prisma` and `@prisma/client` (v5.22.0) with full support for `postgresqlExtensions` preview feature and `prisma validate`.

---

## 3. Database Connection Configuration

All database credentials and connection parameters are loaded dynamically via environment variables without hardcoding.

### Environment Template (`.env.example`)
```bash
# Relational Database (PostgreSQL 16+ with pgvector)
DATABASE_URL="postgresql://postgres:password@localhost:5432/agentgraph_db?schema=public"
DIRECT_URL="postgresql://postgres:password@localhost:5432/agentgraph_db?schema=public"
POSTGRES_HOST="localhost"
POSTGRES_PORT="5432"
POSTGRES_USER="postgres"
POSTGRES_PASSWORD="password"
POSTGRES_DB="agentgraph_db"
```

---

## 4. Prisma Schema Specification

The primary schema resides at [`backend/schema.prisma`](file:///home/mercury/AgentGraph/backend/schema.prisma):

```prisma
// Prisma schema for Enterprise Knowledge Agent – Database Infrastructure
// Database: PostgreSQL 16+ with pgvector extension

generator client {
  provider        = "prisma-client-js"
  previewFeatures = ["postgresqlExtensions"]
}

datasource db {
  provider   = "postgresql"
  url        = env("DATABASE_URL")
  directUrl  = env("DIRECT_URL")
  extensions = [vector]
}

/// Core infrastructure model for database health checks and connectivity verification.
/// Business models (IAM, documents, embeddings, search indexes) are reserved for subsequent modules.
model DatabaseHealth {
  id        String   @id @default(cuid())
  status    String   @default("HEALTHY")
  checkedAt DateTime @default(now()) @map("checked_at")

  @@map("database_health")
}
```

---

## 5. Migration Directory & Operational Workflow

### Migration Directory Structure
```
backend/
├── migrations/
│   ├── 20261117100319_init_database_infrastructure/
│   │   └── migration.sql
│   └── migration_lock.toml
└── schema.prisma
```

### Standard Developer Workflow
Always activate the Conda environment first:
```bash
conda activate ml-env
```

| Action | Command | Description |
| :--- | :--- | :--- |
| **Validate Schema** | `npm run prisma:validate` | Validates schema syntax and connection references against PostgreSQL rules |
| **Format Schema** | `npm run prisma:format` | Auto-formats indentation and model formatting |
| **Generate Client** | `npm run prisma:generate` | Emits typed `@prisma/client` artifacts into `backend/node_modules` |
| **Create Migration** | `npm run prisma:migrate` | Runs `prisma migrate dev` to generate and apply versioned SQL migrations |
| **Deploy Migration** | `npm run prisma:migrate:deploy` | Applies unapplied migrations in CI/CD or production environments |
| **Check Status** | `npm run prisma:status` | Reports migration alignment between database and migration files |

---

## 6. Starting PostgreSQL 16+

### Option A: Native Server (within `conda activate ml-env`)
```bash
# Start PostgreSQL cluster
pg_ctl -D postgres_data -l postgres_data/server.log start

# Stop PostgreSQL cluster
pg_ctl -D postgres_data stop

# Check status
pg_ctl -D postgres_data status
```

### Option B: Docker Compose Stack
```bash
docker-compose up -d postgres
```
