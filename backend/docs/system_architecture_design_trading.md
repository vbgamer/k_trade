# B2B2C Multi-Tenant SaaS Trading Platform: Architecture & Design

This specification outlines the architecture, database schemas, and service topologies for a B2B2C Multi-Tenant Options Trading Platform with Strategy Marketplaces, Partner Dashboards, and White-Label branding integrations.

---

## 1. System Topology & Multi-Tenant Deployment Blueprint

The system incorporates a **Tenant Host Resolver** middleware. Requests from custom domains resolve branding, security bounds, and database isolation keys. Strategy executors utilize a **Signal Fan-out Hub** to route parent signals into concurrent broker trades for all sub-accounts.

```mermaid
graph TD
    %% Custom Domains and Gateway
    Client1[Client Domain: alpha.trade.com] -->|HTTPS| Nginx[Nginx Gateway]
    Client2[Partner Domain: beta.partner.com] -->|HTTPS| Nginx
    NormalUser[Trader: mainplatform.com] -->|HTTPS| Nginx
    
    %% API Host Resolver
    Nginx -->|Host headers: main, alpha, beta| TenantResolver[Tenant Host Resolver Middleware]
    TenantResolver -->|Resolve Branding / Scope| APIService[API Gateway / FastAPI]
    
    %% Database and Cache
    APIService <-->|Cache Tenant config / LTPs| Redis[(Redis Cache)]
    APIService <-->|SaaS Registry / Tenant accounts| Postgres[(PostgreSQL DB)]
    
    %% Worker & Scheduler
    APIService -->|Enqueue Actions| RabbitMQ{RabbitMQ Broker}
    SchedulerService[Holidays & Window Scheduler] -->|Scheduled Runs| RabbitMQ
    
    %% Signal Fan-out Copy-Trading Engine
    RabbitMQ -->|Durable Tasks| CeleryWorkers[Celery Worker Cluster]
    CeleryWorkers -->|Spawns Runtime| StrategyRuntime[Strategy Runtime Host]
    StrategyRuntime -->|Signal Triggered| FanOutHub[Copy-Trading Fan-out Executor]
    
    %% Fan-out Execution
    FanOutHub -->|1. Fetch Subscribed Clients| Postgres
    FanOutHub -->|2. Check Active Wallets & RMS| RMSService[Risk Management Service]
    FanOutHub -->|3. Route Orders Parallel| ExecutionService[Execution Service]
    
    %% Gateway to Exchanges
    ExecutionService -->|Dispatch Orders| BrokerGateway[Broker Adapter Gateway]
    BrokerGateway -->|Zerodha Kite / Shoonya / Simulated| Exchanges[Exchange APIs]
    
    %% Monitoring
    APIService & StrategyRuntime & CeleryWorkers -->|Health / Metrics| Prometheus[Prometheus Collector]
    Prometheus --> Grafana[Grafana Dashboard]
```

---

## 2. Service Interaction Sequences

### Sequence A: Multi-User Copy-Trading Signal Fan-out

```mermaid
sequenceDiagram
    autonumber
    participant Ex as NSE Exchange
    participant MDS as Market Data Service
    participant Bus as Redis Pub/Sub
    participant SR as Strategy Runtime
    participant FO as Fan-out Hub
    participant DB as Postgres DB
    participant ES as Execution Service
    participant AL as Audit Logger
    participant BG as Broker Adapter Gateway
    participant WS as WebSocket Handler
    participant UI as Client Dashboards

    Ex->>MDS: Options Tick
    MDS->>MDS: Construct closed candle
    MDS->>Bus: Publish Candle
    Bus->>SR: Deliver Candle
    SR->>SR: Calculate Crossover Indicators
    Note over SR: Strategy Signal Triggered: BUY Call Option
    
    SR->>FO: Forward Signal (Strategy Version ID, Parameters)
    activate FO
    FO->>DB: Query Active Licensed Subscriptions (Find client accounts)
    DB-->>FO: List of Active Subscribed Clients (User IDs, Allocations, Multipliers)
    
    Note over FO: Begin Concurrent Multi-User Execution Loop
    
    par Client A Execution
        FO->>ES: Route Client A Trade (User A, Qty x Multiplier, Key)
        activate ES
        ES->>AL: Log Pre-Trade (User A ID)
        ES->>BG: Dispatch Order to User A's connected Broker API
        BG-->>ES: Order ID
        ES-->>FO: Order Dispatched
        deactivate ES
    and Client B Execution
        FO->>ES: Route Client B Trade (User B, Qty x Multiplier, Key)
        activate ES
        ES->>AL: Log Pre-Trade (User B ID)
        ES->>BG: Dispatch Order to User B's connected Broker API
        BG-->>ES: Order ID
        ES-->>FO: Order Dispatched
        deactivate ES
    end
    
    FO-->>SR: Fan-out Completed
    deactivate FO
    
    %% Broadcast to WebSockets
    ES->>WS: Broadcast Execution updates
    WS->>UI: Real-time update on Client dashboards (Buy Filled at ₹150)
```

---

## 3. Database Schema Extensions (B2B2C Multi-Tenant ERD)

```mermaid
erDiagram
    TENANTS ||--o{ WHITE_LABEL_CONFIGS : "customizes"
    TENANTS ||--o{ USERS : "hosts"
    TENANTS ||--o{ STRATEGY_DEFINITIONS : "registers"
    
    USERS ||--o{ USER_ROLES : "has"
    USERS ||--o{ BROKER_CREDENTIALS : "has"
    USERS ||--o{ STRATEGY_LICENSES : "owns"
    
    PARTNERS ||--|{ USERS : "is"
    PARTNERS ||--o{ STRATEGY_DEFINITIONS : "authorizes"
    PARTNERS ||--o{ STRATEGY_LICENSES : "grants"
    PARTNERS ||--o{ CLIENT_RELATIONS : "manages"
    
    STRATEGY_DEFINITIONS ||--o{ STRATEGY_VERSIONS : "has"
    STRATEGY_VERSIONS ||--o{ STRATEGY_SUBSCRIPTIONS : "runs"
    
    STRATEGY_SUBSCRIPTIONS ||--o{ POSITIONS : "updates"
    STRATEGY_SUBSCRIPTIONS ||--o{ BROKER_ORDERS : "places"

    TENANTS {
        uuid id PK
        string slug UK
        string company_name
        boolean is_active
        timestamp created_at
    }

    WHITE_LABEL_CONFIGS {
        uuid id PK
        uuid tenant_id FK
        string custom_domain UK
        string logo_url
        string primary_color
        string accent_color
        string smtp_host
        string support_email
        timestamp updated_at
    }

    USERS {
        uuid id PK
        uuid tenant_id FK
        string email UK
        string password_hash
        string first_name
        string last_name
        boolean is_active
        timestamp created_at
    }

    USER_ROLES {
        uuid id PK
        uuid user_id FK
        string role_name "admin | partner | client"
    }

    PARTNERS {
        uuid id PK
        uuid user_id FK
        string brand_name
        float platform_margin_commission
        timestamp registered_at
    }

    CLIENT_RELATIONS {
        uuid id PK
        uuid partner_id FK
        uuid client_user_id FK
        string status "active | suspended"
        timestamp created_at
    }

    STRATEGY_DEFINITIONS {
        uuid id PK
        uuid tenant_id FK
        uuid creator_partner_id FK
        string slug UK
        string name
        boolean is_public
        boolean licensing_enabled
        timestamp created_at
    }

    STRATEGY_LICENSES {
        uuid id PK
        uuid partner_id FK
        uuid strategy_definition_id FK
        uuid target_client_user_id FK
        string access_permissions "view | execute | subscribe"
        float license_fee
        timestamp expires_at
    }
```

---

## 4. API Endpoints Contract (B2B2C / Tenant Extensions)

Exposes partner dashboards, licensing configurations, and white-label management.

```yaml
  /api/v1/tenant/white-label:
    get:
      summary: Retrieve custom White-Label configurations based on Host Header
      responses:
        200:
          content:
            application/json:
              schema:
                type: object
                properties:
                  company_name: { type: string }
                  logo_url: { type: string }
                  primary_color: { type: string }
                  accent_color: { type: string }
                  custom_domain: { type: string }

  /api/v1/partner/clients:
    get:
      summary: Get all clients registered under the partner account
      security: [{ BearerAuth: [] }]
      responses:
        200:
          content:
            application/json:
              schema:
                type: array
                items:
                  type: object
                  properties:
                    client_id: { type: string, format: uuid }
                    email: { type: string }
                    status: { type: string }
                    active_subscriptions_count: { type: integer }

  /api/v1/partner/licenses:
    post:
      summary: License a strategy definition to a specific client user
      security: [{ BearerAuth: [] }]
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [strategy_definition_id, client_user_id]
              properties:
                strategy_definition_id: { type: string, format: uuid }
                client_user_id: { type: string, format: uuid }
                license_fee: { type: number }
                expires_at: { type: string, format: date-time }
      responses:
        201:
          description: License created and strategy authorized for client
```
