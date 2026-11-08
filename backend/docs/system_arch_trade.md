# Enterprise Options Trading Platform: System Architecture & Design

This document details the architecture, service interactions, data models, API contracts, and deployment blueprints for the production-grade SaaS Options Trading Platform MVP.

---

## 1. System Architecture & Docker Deployment Blueprint
The platform uses a modular, service-oriented architecture. Services communicate asynchronously using **RabbitMQ** (for reliable commands/jobs) and **Redis** (for fast pub/sub market ticks and session states).

```mermaid
graph TD
    %% Clients
    UserBrowser[User Browser / React App] -->|HTTPS / WSS| APIGateway[API Gateway / Nginx]
    
    %% Gateway to API
    APIGateway -->|Route Requests| APIService[API Service / FastAPI]
    
    %% API to DB and Services
    APIService <-->|Read/Write| Postgres[(PostgreSQL DB)]
    APIService <-->|Cache / WS State| RedisCache[(Redis Cache & Session)]
    APIService -->|Enqueues Jobs| RabbitMQ{RabbitMQ Message Broker}
    
    %% Event Drivers & Workers
    RabbitMQ -->|Durable Tasks| CeleryWorker[Celery Task Workers]
    SchedulerService[Scheduler Service / Celery Beat] -->|Cron Markets Open-Close| RabbitMQ
    
    %% Market Pipeline
    Exchange[NSE Exchange API] -->|Websocket Ticks| MarketDataService[Market Data Feed Service]
    MarketDataService -->|Aggregate Candles| RedisCache
    MarketDataService -->|Candle Closed Event| PubSub[Redis Pub/Sub]
    
    %% Core Engines
    PubSub -->|Stream Ticks/Candles| StrategyEngine[Strategy Execution Engine]
    StrategyEngine <-->|Verify Settings| Postgres
    StrategyEngine -->|Validate & Validate RMS| RMS[Risk Management Service]
    StrategyEngine -->|Submit Order Command| ExecutionService[Order Execution Service]
    
    %% Execution & Adapters
    ExecutionService -->|Idempotency / Audit / Slippage| BrokerGateway[Broker Adapter Gateway]
    BrokerGateway -->|API Call| Broker[Broker API: Shoonya / Kite]
    
    %% Monitoring
    APIService & StrategyEngine & ExecutionService & CeleryWorker -->|Expose Metrics| Prometheus[Prometheus Collector]
    Prometheus --> Grafana[Grafana Dashboard]
    FluentBit[FluentBit Log Daemon] -->|Pushes Logs| Loki[Grafana Loki]
    Loki --> Grafana
```

---

## 2. Service Interaction & Sequence Diagram
This sequence diagram demonstrates the flow from a real-time market tick to order execution and state update.

```mermaid
sequenceDiagram
    autonumber
    participant Ex as NSE Exchange
    participant MDS as Market Data Service
    participant Redis as Redis Pub/Sub
    participant SE as Strategy Engine
    participant RMS as Risk Service (RMS)
    participant ES as Execution Service
    participant BG as Broker Adapter Gateway
    participant DB as Postgres DB
    participant WS as WebSocket Handler
    participant UI as React UI

    Ex->>MDS: Raw WebSocket Ticks
    MDS->>MDS: Candle Construction (Aggregate Tick stream)
    Note over MDS: 1-Minute Candle Closes
    MDS->>Redis: Publish "CANDLE_CLOSED" Event
    Redis->>SE: Broadcast Event to Strategy Listeners
    activate SE
    SE->>SE: Transform to Heikin Ashi
    SE->>SE: Calculate WMA(5) vs SMA(1)
    Note over SE: Bullish Crossover Signal Detected
    SE->>RMS: Verify Risk Rules (Max Position / Max Loss limit check)
    RMS-->>SE: Risk Check Approved
    SE->>ES: Route Order Intent (Strategy ID, User ID, Option Contract, Side, Qty)
    deactivate SE
    activate ES
    ES->>ES: Generate Idempotency Key
    ES->>ES: Perform Pre-Order Validation (Margin/Slippage Limits)
    ES->>DB: Log Trade record as "PENDING_EXECUTION"
    ES->>BG: Route Order Request (Kite/Shoonya adapter)
    activate BG
    BG->>Ex: Submit Order (REST Request)
    Ex-->>BG: Return Order ID (Status: PENDING)
    BG-->>ES: Return Broker Order ID
    deactivate BG
    ES->>WS: Broadcast "OrderPlaced" event
    WS->>UI: Stream Event: OrderPlaced (Neon Cyan Banner)
    deactivate ES
    
    Note over Ex: Exchange Fills Order
    Ex->>BG: Order Update Feed Event (FILLED, Price=150.2)
    activate BG
    BG->>ES: Async Order Update (Broker ID, State: FILLED, Fill Price)
    deactivate BG
    activate ES
    ES->>DB: Update Trade record as "EXECUTED", Fill Price=150.2
    ES->>WS: Broadcast "OrderFilled" event
    WS->>UI: Stream Event: OrderFilled (Sound & Neon Green Notification)
    deactivate ES
```

---

## 3. Database Schema (Entity Relationship Diagram)
The database structure supports tenant segregation, strategy registries, audit trails, and version control.

```mermaid
erDiagram
    USERS ||--o{ USER_SESSIONS : "creates"
    USERS ||--o{ BROKER_CREDENTIALS : "has"
    USERS ||--o{ STRATEGY_SUBSCRIPTIONS : "subscribes"
    USERS ||--o{ AUDIT_LOGS : "triggers"
    USERS ||--o{ NOTIFICATIONS : "receives"
    
    STRATEGY_DEFINITIONS ||--o{ STRATEGY_VERSIONS : "has"
    STRATEGY_VERSIONS ||--o{ STRATEGY_SUBSCRIPTIONS : "implements"
    
    STRATEGY_SUBSCRIPTIONS ||--o{ POSITIONS : "maintains"
    STRATEGY_SUBSCRIPTIONS ||--o{ BROKER_ORDERS : "executes"
    
    BROKER_ORDERS ||--o{ TRADES : "generates"
    BROKER_ORDERS ||--o{ AUDIT_LOGS : "logs"

    USERS {
        uuid id PK
        string email UK
        string password_hash
        string first_name
        string last_name
        boolean is_active
        timestamp created_at
    }

    USER_SESSIONS {
        uuid id PK
        uuid user_id FK
        string token UK
        string ip_address
        string user_agent
        timestamp expires_at
        timestamp created_at
    }

    BROKER_CREDENTIALS {
        uuid id PK
        uuid user_id FK
        string broker_name
        string api_key_encrypted
        string api_secret_encrypted
        string client_id_encrypted
        string password_encrypted
        string totp_secret_encrypted
        timestamp updated_at
    }

    STRATEGY_DEFINITIONS {
        uuid id PK
        string slug UK
        string name
        string description
        string category
        boolean is_public
        uuid creator_id FK
        timestamp created_at
    }

    STRATEGY_VERSIONS {
        uuid id PK
        uuid strategy_definition_id FK
        string semver
        string parameters_schema_json
        string runtime_code_hash
        timestamp released_at
    }

    STRATEGY_SUBSCRIPTIONS {
        uuid id PK
        uuid user_id FK
        uuid strategy_version_id FK
        string status
        float max_profit
        float max_loss
        integer quantity
        string product_type
        jsonb config_json
        timestamp created_at
    }

    POSITIONS {
        uuid id PK
        uuid strategy_subscription_id FK
        string instrument_key UK
        string trading_symbol
        integer quantity
        float average_price
        float unrealized_pnl
        float realized_pnl
        timestamp updated_at
    }

    BROKER_ORDERS {
        uuid id PK
        uuid strategy_subscription_id FK
        string client_order_id UK
        string broker_order_id UK
        string side
        string instrument_key
        string order_type
        integer quantity
        string product_type
        string status
        string rejection_reason
        timestamp created_at
    }

    TRADES {
        uuid id PK
        uuid broker_order_id FK
        float execution_price
        integer quantity
        float slippage
        float brokerage_fees
        timestamp executed_at
    }

    AUDIT_LOGS {
        uuid id PK
        uuid user_id FK
        string event_type
        string message
        jsonb metadata
        timestamp created_at
    }

    NOTIFICATIONS {
        uuid id PK
        uuid user_id FK
        string type
        string title
        string body
        boolean is_read
        timestamp created_at
    }

    SYSTEM_SETTINGS {
        string key PK
        string value
        string description
        timestamp updated_at
    }
```

---

## 4. API Specification (OpenAPI Endpoint Contract)

Below is the structured REST API specification mapping the core platform features.

```yaml
openapi: 3.0.3
info:
  title: SaaS Options Trading Platform API
  version: 1.0.0
  description: REST API for multi-tenant options trading automation, broker credentials management, strategy subscriptions, and execution monitoring.
paths:
  /api/v1/auth/register:
    post:
      summary: Register a new user
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [email, password, first_name, last_name]
              properties:
                email: { type: string, format: email }
                password: { type: string, format: password }
                first_name: { type: string }
                last_name: { type: string }
      responses:
        201:
          description: User registered successfully
  /api/v1/auth/login:
    post:
      summary: User Login
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [email, password]
              properties:
                email: { type: string, format: email }
                password: { type: string }
      responses:
        200:
          description: Returns authentication token
          content:
            application/json:
              schema:
                type: object
                properties:
                  access_token: { type: string }
                  token_type: { type: string, example: bearer }

  /api/v1/broker/credentials:
    post:
      summary: Save encrypted broker API credentials
      security: [{ BearerAuth: [] }]
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [broker_name, api_key, api_secret, client_id, password]
              properties:
                broker_name: { type: string, enum: [kite, shoonya] }
                api_key: { type: string }
                api_secret: { type: string }
                client_id: { type: string }
                password: { type: string }
                totp_secret: { type: string }
      responses:
        200:
          description: Credentials saved and encrypted successfully

  /api/v1/strategies:
    get:
      summary: Retrieve all registered strategies in the marketplace
      responses:
        200:
          content:
            application/json:
              schema:
                type: array
                items:
                  type: object
                  properties:
                    id: { type: string, format: uuid }
                    slug: { type: string }
                    name: { type: string }
                    description: { type: string }
                    versions:
                      type: array
                      items:
                        type: object
                        properties:
                          id: { type: string, format: uuid }
                          semver: { type: string }

  /api/v1/subscriptions:
    post:
      summary: Subscribe a user to a strategy version
      security: [{ BearerAuth: [] }]
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [strategy_version_id, quantity, product_type]
              properties:
                strategy_version_id: { type: string, format: uuid }
                quantity: { type: integer, minimum: 1 }
                product_type: { type: string, enum: [MIS, NRML] }
                max_profit: { type: number }
                max_loss: { type: number }
                config_json: { type: object }
      responses:
        201:
          description: Subscription created successfully

  /api/v1/subscriptions/{id}/toggle:
    post:
      summary: Start or Stop strategy execution loop
      security: [{ BearerAuth: [] }]
      parameters:
        - name: id
          in: path
          required: true
          schema: { type: string, format: uuid }
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [active]
              properties:
                active: { type: boolean }
      responses:
        200:
          description: Subscription status toggled

  /api/v1/portfolio/positions:
    get:
      summary: Get active open positions and calculated unrealized MTM
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
                    instrument_key: { type: string }
                    trading_symbol: { type: string }
                    quantity: { type: integer }
                    average_price: { type: number }
                    unrealized_pnl: { type: number }
                    realized_pnl: { type: number }

components:
  securitySchemes:
    BearerAuth:
      type: http
      scheme: bearer
      bearerFormat: JWT
```

---

## 5. Module Boundaries & Repository Directory Layout
To satisfy decoupled service-oriented architecture, backend structures are segregated as follows:

```
backend/
├── app/
│   ├── api/                 # FastAPI controllers (auth, strategies, orders, portfolio)
│   ├── core/                # Configuration (config.py, security.py, database.py)
│   ├── broker/              # Broker Adapters (base_adapter.py, kite.py, shoonya.py)
│   ├── db/                  # SQL Alchemy Models & Migration scripts (Alembic)
│   ├── engine/              # Decoupled Engine Components
│   │   ├── runtime.py       # Generic Strategy Runtime (Plugin loader interface)
│   │   ├── registry.py      # Strategy Discovery & Registry logic
│   │   ├── market_data.py   # WebSocket Streamer & Candle Aggregator
│   │   └── rms.py           # Independent Risk Management System (Circuit breaker)
│   ├── services/            # Business Logic Layers
│   │   ├── execution.py     # Independent Execution Service (retry, validation, slippage)
│   │   ├── portfolio.py     # Independent Portfolio & Realized/Unrealized P&L Service
│   │   └── scheduler.py     # Celery Beat Scheduled task coordinator
│   ├── workers/             # Celery Workers & RabbitMQ task queues
│   │   ├── tasks.py         # Asynchronous background tasks
│   │   └── celery_app.py    # Celery instantiation config
│   └── monitoring/          # Prometheus gauges, counters, health logs
└── tests/                   # Pytest automation suite
```

---

## 6. Structured WebSocket Events Contract
Real-time state transfers utilize structured WebSocket JSON payloads over `wss://<host>/api/v1/ws/stream`. All events contain `event_type`, `timestamp`, and structured `data` objects.

```json
/* Event 1: StrategyStarted */
{
  "event_type": "StrategyStarted",
  "timestamp": "2026-07-09T14:10:00.000Z",
  "data": {
    "subscription_id": "a98fd2b8-936d-4952-ba63-549179d638ac",
    "strategy_slug": "kaushalcustomWMASMACall1",
    "version": "1.0.0",
    "user_id": "c1f72a6b-c743-4e3f-a63d-4903f71c4c1a",
    "timeframe_min": 1,
    "quantity": 65
  }
}

/* Event 2: SignalGenerated */
{
  "event_type": "SignalGenerated",
  "timestamp": "2026-07-09T14:11:00.000Z",
  "data": {
    "subscription_id": "a98fd2b8-936d-4952-ba63-549179d638ac",
    "signal": "BUY",
    "instrument_key": "NSE_INDEX|Nifty 50",
    "close_price": 24350.50,
    "indicators": {
      "wma_close_val": 24352.10,
      "sma_open_val": 24351.05
    }
  }
}

/* Event 3: OrderPlaced */
{
  "event_type": "OrderPlaced",
  "timestamp": "2026-07-09T14:11:01.200Z",
  "data": {
    "client_order_id": "co_981bdf93_20a8",
    "broker_order_id": "BO-1002931-KITE",
    "subscription_id": "a98fd2b8-936d-4952-ba63-549179d638ac",
    "side": "BUY",
    "instrument_key": "NSE_OPTION|Nifty50_24350_CE",
    "quantity": 65,
    "product_type": "MIS",
    "order_type": "LIMIT",
    "limit_price": 145.50
  }
}

/* Event 4: OrderFilled */
{
  "event_type": "OrderFilled",
  "timestamp": "2026-07-09T14:11:02.100Z",
  "data": {
    "client_order_id": "co_981bdf93_20a8",
    "broker_order_id": "BO-1002931-KITE",
    "status": "FILLED",
    "fill_price": 145.45,
    "slippage": -0.05
  }
}

/* Event 5: PnLUpdated */
{
  "event_type": "PnLUpdated",
  "timestamp": "2026-07-09T14:12:00.000Z",
  "data": {
    "subscription_id": "a98fd2b8-936d-4952-ba63-549179d638ac",
    "unrealized_pnl": 450.00,
    "realized_pnl": 0.00,
    "current_ltp": 152.35
  }
}

/* Event 6: RiskTriggered */
{
  "event_type": "RiskTriggered",
  "timestamp": "2026-07-09T14:15:30.000Z",
  "data": {
    "subscription_id": "a98fd2b8-936d-4952-ba63-549179d638ac",
    "trigger_type": "MAX_LOSS_LIMIT",
    "current_mtm": -5020.50,
    "limit_threshold": -5000.00,
    "action_taken": "AUTO_FLATTEN_AND_HALT"
  }
}
```

---

## 7. Cloud-Native CI/CD Pipeline Configuration (GitHub Actions)
Deployments target **Oracle Cloud Infrastructure (OCI)** using container registries and Kubernetes (OKE) or VM nodes.

```yaml
# .github/workflows/deploy.yml
name: SaaS Options Engine OCI Deploy

on:
  push:
    branches: [ main ]

jobs:
  test-and-lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - name: Install dependencies
        run: |
          pip install -r backend/requirements.txt
          pip install pytest flake8 black
      - name: Lint code
        run: |
          flake8 backend/
          black --check backend/
      - name: Run Pytest
        run: pytest backend/tests/

  build-and-push:
    needs: test-and-lint
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Install OCI CLI
        run: |
          mkdir -p ~/.oci
          echo "${{ secrets.OCI_CONFIG }}" > ~/.oci/config
          echo "${{ secrets.OCI_KEY_FILE }}" > ~/.oci/oci_api_key.pem
          chmod 600 ~/.oci/config ~/.oci/oci_api_key.pem
      - name: OCI Docker Authentication
        run: |
          echo "${{ secrets.OCI_AUTH_TOKEN }}" | docker login ${{ secrets.OCI_REGISTRY_URL }} --username ${{ secrets.OCI_USER }} --password-stdin
      - name: Build and Push Backend Image
        run: |
          docker build -t ${{ secrets.OCI_REGISTRY_URL }}/saas-options-api:${{ github.sha }} -f backend/Dockerfile .
          docker push ${{ secrets.OCI_REGISTRY_URL }}/saas-options-api:${{ github.sha }}

  deploy:
    needs: build-and-push
    runs-on: ubuntu-latest
    steps:
      - name: Trigger SSH deployment to OCI VM
        uses: appleboy/ssh-action@master
        with:
          host: ${{ secrets.OCI_VM_IP }}
          username: opc
          key: ${{ secrets.OCI_VM_SSH_KEY }}
          script: |
            docker pull ${{ secrets.OCI_REGISTRY_URL }}/saas-options-api:${{ github.sha }}
            cd ~/platform && docker-compose down
            sed -i 's|saas-options-api:.*|saas-options-api:${{ github.sha }}|g' docker-compose.yml
            docker-compose up -d
            docker system prune -f
```
