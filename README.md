# FunHub API

Centralized Python FastAPI backend for all FunHub games (MixMo, QuizMo, etc.)

## Features

- **Unified Credits System** - Credits work across all games
- **OTP Authentication** - Passwordless email verification
- **Cross-Device Sync** - Link account via email
- **Anti-Cheat** - Signed game sessions with score validation
- **PayPal Integration** - Server-side payment verification
- **Leaderboards** - Daily, weekly, and all-time rankings

## API Endpoints

### Health
- `GET /health` - Health check

### Players
- `POST /players/register` - Register anonymous player
- `GET /players/me` - Get player info

### Authentication
- `POST /auth/request-otp` - Request 6-digit OTP code
- `POST /auth/verify-otp` - Verify OTP and get session token

### Credits
- `GET /credits` - Get credit balance
- `POST /credits/use` - Use credits
- `POST /credits/verify-purchase` - Verify PayPal payment

### Games
- `POST /games/{game}/start` - Start game session (returns anti-cheat token)

### Leaderboard
- `GET /leaderboard/{game}` - Get leaderboard (daily/weekly/alltime)
- `POST /leaderboard/{game}/submit` - Submit score with session token
- `GET /leaderboard/{game}/me` - Get player's rank

## Local Development

```bash
# Create virtual environment
python -m venv .venv

# Activate (Windows)
.\.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt

# Copy environment file
cp .env.example .env
# Edit .env with your credentials

# Run server
uvicorn app.main:app --reload --port 8000
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_KEY` | Supabase anon key |
| `JWT_SECRET` | Secret for signing session tokens |
| `ENVIRONMENT` | `development` or `production` |
| `PAYPAL_CLIENT_ID` | PayPal app client ID |
| `PAYPAL_CLIENT_SECRET` | PayPal app client secret |

## Deployment to Render

1. Push code to GitHub
2. Create new Web Service on [Render](https://render.com)
3. Connect your GitHub repo
4. Set root directory to `funhub-api`
5. Set build command: `pip install -r requirements.txt`
6. Set start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
7. Add environment variables in Render dashboard
8. Deploy!

### Keep-Alive (Free Tier)

Render free tier spins down after 15 minutes of inactivity. We use GitHub Actions to ping the health endpoint every 14 minutes.

1. Add `FUNHUB_API_URL` secret to your GitHub repo (e.g., `https://funhub-api.onrender.com`)
2. The `.github/workflows/keep-alive.yml` will automatically run

## Database Migration

Run the SQL migration in Supabase SQL Editor:
- `docs/supabase-migration-v2.sql`

This creates: `accounts`, `players`, `otp_codes`, `game_sessions`, `used_order_ids`, `credit_transactions`, `leaderboards`

## API Documentation

Once running, visit:
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc
