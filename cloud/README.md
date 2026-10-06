# CapWiz — Cloud (licensing backend)

Backend separato per **account + abbonamenti Stripe + validazione licenza**.
L'app desktop lo interroga per il login e per sapere se l'abbonamento è attivo.

Questo NON è il sidecar locale (che gira sul PC dell'utente e fa il montaggio).
Questo si **deploya** (Railway / Fly / Render / VPS) ed è raggiungibile via HTTPS.

## Endpoint

| Metodo | Path | Descrizione |
|---|---|---|
| GET | `/health` | stato + se Stripe è configurato |
| POST | `/auth/register` | `{email, password}` → `{token}` (JWT) |
| POST | `/auth/login` | `{email, password}` → `{token}` |
| GET | `/me` | stato utente + `active` (richiede Bearer token) |
| POST | `/billing/checkout` | crea sessione Stripe Checkout → `{url}` |
| POST | `/billing/portal` | crea sessione billing portal → `{url}` |
| POST | `/webhooks/stripe` | webhook Stripe (aggiorna lo stato abbonamento) |

## Sviluppo locale

```bash
cd cloud
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # e compila i valori
JWT_SECRET=dev .venv/bin/uvicorn app.main:app --reload --port 8799
```

## Stripe

1. Crea un **Product** con un **Price ricorrente** (es. 3,99€/mese) → copia il `price_...` in `STRIPE_PRICE_ID`.
2. `STRIPE_SECRET_KEY` dalla dashboard (usa `sk_test_...` in sviluppo).
3. Webhook: in dev usa la Stripe CLI
   ```bash
   stripe listen --forward-to localhost:8799/webhooks/stripe
   ```
   copia il `whsec_...` in `STRIPE_WEBHOOK_SECRET`. In prod, crea il webhook nella
   dashboard puntando a `https://tuo-dominio/webhooks/stripe` con gli eventi
   `checkout.session.completed`, `customer.subscription.updated`,
   `customer.subscription.deleted`.

## Deploy

- `DATABASE_URL` → Postgres in produzione (sqlite va bene solo per test locale).
- Metti tutte le variabili di `.env.example` come secret dell'hosting.
- L'app desktop punta qui via `CAPWIZ_CLOUD_URL` (vedi `desktop/electron/main.js`).

## Sicurezza / note

- Password con **bcrypt**, token **JWT** firmati con `JWT_SECRET`.
- Webhook Stripe con **verifica firma** (`STRIPE_WEBHOOK_SECRET`).
- L'app desktop tiene un **grace period offline** di 7 giorni: se il cloud non è
  raggiungibile ma l'ultimo check era attivo, continua a funzionare.
- **IVA**: con Stripe diretto la gestione IVA UE (OSS/MOSS) è a carico tuo —
  valuta Stripe Tax e le dichiarazioni. (Un Merchant of Record come Lemon
  Squeezy/Paddle la gestirebbe al posto tuo.)
