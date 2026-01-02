import os
import jwt
import json
import uuid
import httpx
import dotenv
import base64
import secrets
import hashlib
import asyncio
import berserk
import logging
import pandas as pd
from postgrest import APIError
from typing import Optional, Dict
from urllib.parse import urlencode
from supabase import create_client, Client
from cryptography.fernet import Fernet
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi.responses import RedirectResponse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from fastapi import (
    FastAPI,
    Request,
    Depends,
    HTTPException,
    Response,
    WebSocket,
    WebSocketDisconnect,
)

# own modules
from analysis import generate_insights, stream_games_for_user, extract_features
from llm_service import (
    build_context,
    build_snappy_prompt,
    generate_llm_recommendations,
    get_system_prompt,
)

# Constants
MAX_GAMES_ANALYSIS = 50
ANALYSIS_CACHE_DAYS = 7
ERROR_CACHE_DAYS = 1  # Shorter cache for error cases
SESSION_DURATION_DAYS = 30
MIN_LISTENING_DURATION = 1  # minutes
MAX_LISTENING_DURATION = 60  # minutes
WS_1008_POLICY_VIOLATION = 1008

# Environment configuration (needed for logging setup)
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

# Configure logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "WARNING")

# Custom formatter for structured logging
class ProductionFormatter(logging.Formatter):
    def format(self, record):
        # Add request ID if available
        if hasattr(record, 'request_id'):
            record.msg = f"[{record.request_id}] {record.msg}"
        return super().format(record)

# Configure root logger
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
    ],
)

# Apply custom formatter
for handler in logging.getLogger().handlers:
    handler.setFormatter(ProductionFormatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))

logger = logging.getLogger(__name__)

# Reduce logging verbosity in production
if ENVIRONMENT == "production":
    # Suppress noisy loggers
    logging.getLogger("httpx").setLevel(logging.ERROR)
    logging.getLogger("uvicorn.access").setLevel(logging.ERROR)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.ERROR)
    logging.getLogger("berserk").setLevel(logging.ERROR)
    
    # Only log critical events from external libraries
    logging.getLogger("supabase").setLevel(logging.ERROR)
    logging.getLogger("postgrest").setLevel(logging.ERROR)
else:
    # Development: more verbose
    logging.getLogger("httpx").setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)


############### Setup ###############

"""
Database Schema Requirements:

1. users table:
   - id (text, primary key) - Lichess username
   - access_token (text) - Lichess API token
   - token_expires_at (timestamp) - Token expiration
   - session_expires_at (timestamp) - Session expiration
   - session_token (text) - JWT session token
   - created_at (timestamp) - Account creation time

2. analytics table:
   - game_id (text, primary key) - Unique game identifier
   - created_by (text) - User who created this analysis
   - target_user (text) - Opponent being analyzed
   - color (text) - Color preference for analysis
   - prep_text (text) - Analysis results
   - created_at (timestamp) - Analysis creation time
"""

dotenv.load_dotenv()

# Rate limiting setup
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(
    title="PrepBot-3000 Chess API",
    description="PrepBot-3000 is a chess opponent analysis and preparation tool",
    version="1.0.0",
    docs_url="/docs" if ENVIRONMENT == "development" else None,
    redoc_url="/redoc" if ENVIRONMENT == "development" else None,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# Request ID middleware for better debugging
class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())[:8]
        request.state.request_id = request_id

        # Add request ID to logger context
        if ENVIRONMENT == "development":
            logger.debug(f"Request {request_id}: {request.method} {request.url.path}")
        
        # Log important requests in production (authentication, analysis, etc.)
        if ENVIRONMENT == "production" and request.url.path in [
            "/api/auth/exchange-token", 
            "/api/analyze_user", 
            "/api/start_listening",
            "/api/logout"
        ]:
            logger.info(f"Request {request_id}: {request.method} {request.url.path}")

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


app.add_middleware(RequestIDMiddleware)


# Global exception handler
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", "unknown")
    logger.error(f"Request {request_id}: Unhandled exception: {exc}", exc_info=True)
    return {
        "error": "Internal server error",
        "detail": "An unexpected error occurred",
        "request_id": request_id,
    }


# Environment configuration
SECRET_KEY = os.getenv("SECRET_KEY")
CLIENT_ID = os.getenv("LICHESS_CLIENT_ID")
BASE_URL = os.getenv(
    "BASE_URL", "http://localhost:8000" if ENVIRONMENT == "development" else None
)
FRONTEND_URL = os.getenv(
    "FRONTEND_URL", "http://localhost:3000" if ENVIRONMENT == "development" else None
)
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY")

# Validate required environment variables
required_vars = {
    "SECRET_KEY": SECRET_KEY,
    "LICHESS_CLIENT_ID": CLIENT_ID,
    "BASE_URL": BASE_URL,
    "FRONTEND_URL": FRONTEND_URL,
    "SUPABASE_URL": SUPABASE_URL,
    "SUPABASE_KEY": SUPABASE_KEY,
    "GROQ_API_KEY": GROQ_API_KEY,
    "ENCRYPTION_KEY": ENCRYPTION_KEY,
}

missing_vars = [var for var, value in required_vars.items() if not value]
if missing_vars:
    logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
    raise ValueError(
        f"Missing required environment variables: {', '.join(missing_vars)}"
    )

# Validate environment-specific requirements
if ENVIRONMENT == "production":
    if not BASE_URL or not BASE_URL.startswith("https://"):
        raise ValueError("BASE_URL must be HTTPS in production")
    if not FRONTEND_URL or not FRONTEND_URL.startswith("https://"):
        raise ValueError("FRONTEND_URL must be HTTPS in production")


# Supabase clients
supabase_service: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Use service client for backend operations
supabase: Client = supabase_service

if not ENCRYPTION_KEY:
    raise ValueError("Missing required environment variable: ENCRYPTION_KEY")
try:
    _fernet: Fernet = Fernet(ENCRYPTION_KEY.encode())
except Exception as e:
    raise ValueError(f"Invalid ENCRYPTION_KEY provided: {e}")


def encrypt_token(token: str) -> str:
    if not token:
        return token
    return _fernet.encrypt(token.encode()).decode()


def decrypt_token(encrypted_token: str) -> str:
    if not encrypted_token:
        return encrypted_token
    try:
        return _fernet.decrypt(encrypted_token.encode()).decode()
    except Exception as e:
        raise ValueError("Failed to decrypt access token") from e


def get_user_access_token(user_data: dict) -> Optional[str]:
    raw = user_data.get("access_token") if user_data else None
    return decrypt_token(raw)


# Add CORS middleware for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Session middleware removed - using JWT tokens stored in database instead

PORT = int(os.getenv("PORT", 8000))

# Configure httpx client with connection pooling
httpx_client = httpx.AsyncClient(
    timeout=httpx.Timeout(30.0),
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
    http2=True,
)

executor = ThreadPoolExecutor(max_workers=10)
check_stream_events_queue = {}

# OAuth verifiers removed - using stateless OAuth flow


# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, user_id: str):
        # If user already has a connection, close the old one first
        if user_id in self.active_connections:
            try:
                old_websocket = self.active_connections[user_id]
                await old_websocket.close(code=1000, reason="New connection established")
            except Exception as e:
                logger.warning(f"Error closing old WebSocket for user {user_id}: {e}")
        
        self.active_connections[user_id] = websocket
        if ENVIRONMENT == "development":
            logger.debug(f"WebSocket connected for user: {user_id}")

    def disconnect(self, user_id: str):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
            if ENVIRONMENT == "development":
                logger.debug(f"WebSocket disconnected for user: {user_id}")

    async def send_personal_message(self, message: dict, user_id: str):
        if user_id in self.active_connections:
            try:
                await self.active_connections[user_id].send_text(json.dumps(message))
                # WebSocket message sent
            except Exception as e:
                logger.error(f"Error sending WebSocket message to {user_id}: {e}")
                self.disconnect(user_id)

    async def close_user_connection(self, user_id: str, code: int = 1000, reason: str = "Connection closed"):
        """Gracefully close a user's WebSocket connection"""
        if user_id in self.active_connections:
            try:
                websocket = self.active_connections[user_id]
                await websocket.close(code=code, reason=reason)
            except Exception as e:
                logger.warning(f"Error closing WebSocket for user {user_id}: {e}")
            finally:
                self.disconnect(user_id)


manager = ConnectionManager()

############### Define Functions ###############


def is_error_prep_text(prep_text: str) -> bool:
    """Check if prep_text is an error message (starts with ❌ or ⚠️)"""
    return prep_text.strip().startswith("❌") or prep_text.strip().startswith("⚠️")


def get_cache_duration(prep_text: str) -> int:
    """Get appropriate cache duration based on prep_text content"""
    if is_error_prep_text(prep_text):
        return ERROR_CACHE_DAYS
    return ANALYSIS_CACHE_DAYS


def base64url_encode(data: bytes) -> str:
    """Base64URL encode without padding"""
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def create_verifier() -> str:
    """Create a code verifier for PKCE"""
    return base64url_encode(secrets.token_bytes(32))


def create_challenge(verifier: str) -> str:
    """Create a code challenge from verifier"""
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64url_encode(digest)


def generate_session_token(user_id: str) -> str:
    """Generate a secure session token"""
    payload = {
        "user_id": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(days=30),  # 30 days expiry
        "iat": datetime.now(timezone.utc),
        "type": "session",
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def verify_session_token(token: str) -> Optional[dict]:
    """Verify and decode session token and ensure it matches the active database session.

    This function fails closed: any error validating the token (signature, expiry,
    database access, or mismatch) will return None so the caller can reject the request.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])  # will verify signature and exp

        if payload.get("type") != "session":
            logger.warning("Invalid token type")
            return None

        user_id = payload.get("user_id")
        if not user_id:
            logger.warning("Token missing user_id")
            return None

        # Check DB for active session token for this user
        try:
            result = (
                supabase.table("users")
                .select("session_token, session_expires_at")
                .eq("id", user_id)
                .execute()
            )

            if not result.data or len(result.data) == 0:
                logger.warning(f"No user record found for {user_id}")
                return None

            user_row = result.data[0]
            db_session_token = user_row.get("session_token")
            if db_session_token != token:
                logger.warning(f"Session token mismatch for user {user_id}")
                return None

            # Verify session hasn't expired in DB
            expires_at_str = user_row.get("session_expires_at")
            if expires_at_str:
                try:
                    expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
                    now = datetime.now(timezone.utc)
                    if expires_at < now:
                        logger.warning(f"Session expired for user {user_id}")
                        return None
                except Exception:
                    logger.warning(f"Invalid session_expires_at format for user {user_id}")
                    return None

        except Exception as db_err:
            logger.error(f"Database error while verifying session token for {user_id}: {db_err}")
            # Fail closed - do not allow access if DB cannot be checked
            return None

        return payload

    except jwt.ExpiredSignatureError:
        # Token expired
        return None
    except jwt.InvalidTokenError:
        # Invalid token
        return None


def get_current_user(request: Request) -> Optional[dict]:
    """Get current authenticated user from session token"""
    # Only accept Authorization header - no fallbacks
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        #logger.warning("Missing or invalid Authorization header")
        return None
    
    session_token = auth_header[7:]  # Remove "Bearer " prefix
    
    if not session_token:
        logger.warning("Empty session token in Authorization header")
        return None

    # Verifying session token
    token_payload = verify_session_token(session_token)
    if not token_payload:
        return None

    # Get user data from database
    try:
        result = (
            supabase.table("users")
            .select("*")
            .eq("id", token_payload["user_id"])
            .execute()
        )

        if result.data and len(result.data) > 0:
            user_data = result.data[0]

            if user_data.get("session_token") != session_token:
                logger.warning("Session token mismatch")
                return None

            # Check if session hasn't expired in database
            if user_data.get("session_expires_at"):
                expires_at = datetime.fromisoformat(
                    user_data["session_expires_at"].replace("Z", "+00:00")
                )
                now = datetime.now(timezone.utc)
                if expires_at < now:
                    return None
            return user_data
    except Exception as e:
        logger.error(f"Error fetching user data: {e}")
        return None

    return None


def require_auth(request: Request) -> dict:
    """Dependency to require authentication for protected routes"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


async def get_lichess_token(auth_code: str, verifier: str, redirect_uri: str) -> dict:
    """Exchange authorization code for access token"""
    token_data = {
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
        "client_id": CLIENT_ID,
        "code": auth_code,
        "code_verifier": verifier,
    }

    try:
        response = await httpx_client.post(
            "https://lichess.org/api/token",
            json=token_data,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error getting Lichess token: {e}")
        raise HTTPException(
            status_code=400, detail="Failed to get access token from Lichess"
        )
    except httpx.RequestError as e:
        logger.error(f"Request error getting Lichess token: {e}")
        raise HTTPException(status_code=500, detail="Failed to connect to Lichess")


async def get_lichess_user(access_token: str) -> dict:
    """Get user information from Lichess API"""
    try:
        response = await httpx_client.get(
            "https://lichess.org/api/account",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error getting Lichess user: {e}")
        raise HTTPException(
            status_code=400, detail="Failed to get user info from Lichess"
        )
    except httpx.RequestError as e:
        logger.error(f"Request error getting Lichess user: {e}")
        raise HTTPException(status_code=500, detail="Failed to connect to Lichess")


def get_lichess_client(user_data: dict) -> berserk.Client:
    """Get authenticated Lichess client using user data from database"""
    access_token = get_user_access_token(user_data)
    if not access_token:
        raise HTTPException(status_code=401, detail="No valid access token")

    session = berserk.TokenSession(access_token)
    return berserk.Client(session=session)


async def revoke_lichess_token(access_token: str):
    """Revoke access token from Lichess API"""
    try:
        response = await httpx_client.delete(
            "https://lichess.org/api/token",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        
        # Lichess returns empty response on successful revocation
        # Try to parse JSON, but don't fail if it's empty
        try:
            return response.json() if response.text.strip() else {"message": "Token revoked"}
        except json.JSONDecodeError:
            return {"message": response.text or "Token revoked"}
            
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error revoking Lichess token: {e}")
        # Don't raise exception - logout should succeed even if revocation fails
        return {"error": f"Failed to revoke token: {e}"}
    except Exception as e:
        logger.error(f"Unexpected error revoking Lichess token: {e}")
        return {"error": f"Unexpected error: {e}"}


async def generate_prep(
    opponent_id: str, opponent_color: str, user_access_token: str = None
) -> str:
    """
    Generate comprehensive preparation analysis for an opponent using LLM for snappy recommendations.

    Args:
        opponent_id: Username of the opponent to analyze
        opponent_color: Color the opponent will play ('white' or 'black')
        user_access_token: Access token for Lichess API calls

    Returns:
        String containing actionable preparation advice
    """
    try:
        # Starting analysis for opponent

        if not user_access_token:
            raise ValueError("No user access token provided")

        if not opponent_id or not opponent_color:
            raise ValueError("Opponent ID and color are required")

        if opponent_color not in ["white", "black"]:
            raise ValueError("Opponent color must be 'white' or 'black'")

        # Step 1: Fetch and analyze games (rate limited)
        max_games = MAX_GAMES_ANALYSIS  # Limit to prevent abuse
        all_feats = []

        try:
            async for game in stream_games_for_user(
                opponent_id, user_access_token, opponent_color, max_games=max_games
            ):
                feats = extract_features(game, opponent_color)
                if feats:
                    all_feats.append(feats)
        except httpx.ConnectError as e:
            logger.error(f"Connection error fetching games for {opponent_id}: {e}")
            return f"❌ Could not connect to Lichess to fetch games for {opponent_id}"
        except httpx.RequestError as e:
            logger.error(f"Request error fetching games for {opponent_id}: {e}")
            return f"❌ Error fetching games for {opponent_id}: {str(e)}"

        if not all_feats:
            return f"⚠️ No recent games found for {opponent_id}. Cannot generate preparation analysis."

        df = pd.DataFrame(all_feats)
        # Analysis completed

        # Step 2: Generate insights
        try:
            insights = generate_insights(df, min_info_gain=0.0, top_k=5)
        except Exception as e:
            logger.error(f"Error generating insights for {opponent_id}: {e}")
            return f"❌ Could not analyze game data for {opponent_id}"

        if not insights:
            raise ValueError("No insights generated")

        # Step 3: Build context and prompts
        try:
            context = build_context(insights, opponent_id, opponent_color, len(df))
            prompt = build_snappy_prompt(context)
            system_prompt = get_system_prompt()
        except Exception as e:
            logger.error(f"Error building context for {opponent_id}: {e}")
            return f"❌ Could not prepare analysis context for {opponent_id}"

        # Step 4: generate recommendations
        try:
            quick_analysis = generate_llm_recommendations(
                system_prompt=system_prompt, prompt=prompt, groq_api_key=GROQ_API_KEY
            )
        except Exception as e:
            logger.error(f"Error generating LLM recommendations for {opponent_id}: {e}")
            return f"❌ Could not generate recommendations for {opponent_id}"

        if not quick_analysis:
            raise ValueError("No recommendations generated")

        return quick_analysis

    except ValueError as e:
        logger.error(f"Validation error for {opponent_id}: {e}")
        return f"❌ Invalid input: {str(e)}"
    except Exception as e:
        logger.error(f"Unexpected error generating prep for {opponent_id}: {e}")
        return f"❌ Could not analyze {opponent_id}: {str(e)}"


def listen_for_games_blocking(user: dict, loop: asyncio.AbstractEventLoop):
    user_id = user["id"]
    client = get_lichess_client(user)

    try:
        for event in client.board.stream_incoming_events():
            # check if not expired or canceled
            if user_id in check_stream_events_queue:
                if check_stream_events_queue[user_id]["expiry"] < datetime.now(
                    timezone.utc
                ):
                    # remove from queue
                    check_stream_events_queue.pop(user_id, None)
                    break

            if user_id not in check_stream_events_queue:
                break

            if event.get("type") == "gameStart":
                game_id = event["game"]["fullId"]
                opponent_id = str(event["game"]["opponent"]["id"])
                user_color = event["game"]["color"]
                opponent_color = "white" if user_color == "black" else "black"

                # Get user's access token for API calls (decrypt if needed)
                user_access_token = get_user_access_token(user)

                # Schedule the async part to run on the main event loop
                asyncio.run_coroutine_threadsafe(
                    handle_game_start_event(
                        game_id, user_id, opponent_id, opponent_color, user_access_token
                    ),
                    loop,
                )
                # You could optionally wait for the result, but it's not necessary here
                # future.result()

    except Exception:
        # cancel activity from queue
        check_stream_events_queue.pop(user_id, None)
        # Raising HTTPException here will crash the thread, not send a response.
        # It's better to just log the error.


async def handle_game_start_event(
    game_id: str,
    user_id: str,
    opponent_id: str,
    opponent_color: str,
    user_access_token: str = None,
):
    """Asynchronous handler for game start events."""

    # Check DB if a recent analysis already exists
    # result = supabase.table("analytics").select("*").eq("target_user", opponent_id).execute()
    result = (
        supabase.table("analytics")
        .select("*")
        .eq("target_user", str(opponent_id))
        .eq("color", opponent_color)
        .execute()
    )

    if result.data and len(result.data) > 0:
        user_data = result.data[-1]
        try:
            # Parse the datetime string and ensure it's timezone-aware
            created_at_str = user_data["created_at"]
            if created_at_str.endswith("Z"):
                created_at = datetime.fromisoformat(
                    created_at_str.replace("Z", "+00:00")
                )
            else:
                created_at = datetime.fromisoformat(created_at_str)

            # Ensure both datetimes are timezone-aware for comparison
            now = datetime.now(timezone.utc)
            if created_at.tzinfo is None:
                # If created_at is naive, assume it's UTC
                created_at = created_at.replace(tzinfo=timezone.utc)

            # Use dynamic cache duration based on prep_text content
            cache_days = get_cache_duration(user_data["prep_text"])
            if created_at > now - timedelta(days=cache_days):
                # Send WebSocket notification to the user about analysis
                notification = {
                    "type": "new_analysis",
                    "opponent": str(opponent_id),
                    "color": opponent_color,
                    "game_id": game_id,
                    "message": user_data["prep_text"],
                }
                await manager.send_personal_message(notification, user_id)

                return user_data["prep_text"]
        except (ValueError, TypeError):
            pass  # Error parsing datetime, will generate new analysis

    # Fetch games, analyze, and generate the preparation text
    prep_text = await generate_prep(opponent_id, opponent_color, user_access_token)

    # Store in Supabase
    data = {
        "game_id": game_id,
        "created_by": user_id,
        "target_user": opponent_id,
        "color": opponent_color,
        "prep_text": prep_text,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        supabase.table("analytics").upsert(data, on_conflict="game_id").execute()
        
        # Log successful analysis generation
        logger.info(f"Analysis generated for {opponent_id} ({opponent_color}) by {user_id}")

        # Send WebSocket notification to the user about new analysis
        notification = {
            "type": "new_analysis",
            "opponent": opponent_id,
            "color": opponent_color,
            "game_id": game_id,
            "message": prep_text,
        }
        await manager.send_personal_message(notification, user_id)

    except APIError:
        # Database error - analysis not stored
        logger.error(f"Failed to store analysis for {opponent_id} by {user_id}")
        pass


############### Define API Endpoints ###############


@app.get("/health")
async def health_check():
    """Health check endpoint for deployment monitoring"""
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/auth/oauth-url")
async def get_oauth_url(request: Request):
    """Get OAuth URL for frontend to redirect to Lichess"""
    verifier = create_verifier()
    challenge = create_challenge(verifier)

    oauth_params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": f"{FRONTEND_URL}/callback",
        "scope": "preference:read challenge:read",
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }

    oauth_url = f"https://lichess.org/oauth?{urlencode(oauth_params)}"
    return {"oauth_url": oauth_url, "code_verifier": verifier}


@app.post("/api/auth/exchange-token")
async def exchange_token(request: Request, response: Response, token_data: dict):
    """Exchange OAuth code for access token and create session"""
    try:
        code = token_data.get("code")
        verifier = token_data.get("code_verifier")
        
        if not code:
            raise HTTPException(status_code=400, detail="No authorization code provided")
        
        if not verifier:
            raise HTTPException(status_code=400, detail="No code verifier provided")

        redirect_uri = f"{FRONTEND_URL}/callback"

        lichess_token = await get_lichess_token(code, verifier, redirect_uri)

        if not lichess_token.get("access_token"):
            raise HTTPException(status_code=400, detail="Failed getting token from Lichess")

        access_token = lichess_token["access_token"]
        lichess_user = await get_lichess_user(access_token)
        user_id = lichess_user.get("username")
        if not user_id:
            raise HTTPException(status_code=400, detail="Failed getting user info from Lichess")

        now = datetime.now(timezone.utc)
        token_expires_at = now + timedelta(hours=1)
        session_expires_at = now + timedelta(
            days=SESSION_DURATION_DAYS
        )  # Long-lived session

        # Generate secure session token
        session_token = generate_session_token(user_id)

        data = {
            "id": user_id,
            "access_token": encrypt_token(access_token),
            "token_expires_at": token_expires_at.isoformat(),
            "session_expires_at": session_expires_at.isoformat(),
            "session_token": session_token,  # Store session token in DB
            "created_at": now.isoformat(),
        }

        try:
            supabase.table("users").upsert(data, on_conflict="id").execute()
        except APIError as e:
            raise HTTPException(status_code=500, detail=f"Database error: {e.message}")

        # Log successful authentication
        logger.info(f"User authenticated: {user_id}")

        # Always return session token in response for frontend to handle
        return {
            "success": True, 
            "user_id": user_id, 
            "message": "Authentication successful",
            "session_token": session_token
        }

    except HTTPException:
        raise
    except Exception as e:
        # Catch any unexpected errors
        error_message = f"Unexpected error during token exchange: {str(e)}"
        logger.error(error_message, exc_info=True)
        raise HTTPException(status_code=500, detail=error_message)

'''
@app.get("/auth/callback")
async def callback(request: Request, code: str = None, error: str = None):
    """Handle OAuth callback from Lichess"""
    try:
        # Handle OAuth errors
        if error:
            error_message = f"OAuth error: {error}"
            logger.error(error_message)
            return RedirectResponse(f"{FRONTEND_URL}/callback?error={error_message}")

        if not code:
            error_message = "No authorization code provided"
            logger.error(error_message)
            return RedirectResponse(f"{FRONTEND_URL}/callback?error={error_message}")

        verifier = request.session.get("code_verifier")
        if not verifier:
            error_message = "No code verifier found in session"
            logger.error(error_message)
            return RedirectResponse(f"{FRONTEND_URL}/callback?error={error_message}")

        base_url = BASE_URL
        redirect_uri = f"{base_url}/auth/callback"

        lichess_token = await get_lichess_token(code, verifier, redirect_uri)

        if not lichess_token.get("access_token"):
            error_message = "Failed getting token from Lichess"
            logger.error(error_message)
            return RedirectResponse(f"{FRONTEND_URL}/callback?error={error_message}")

        access_token = lichess_token["access_token"]
        lichess_user = await get_lichess_user(access_token)
        user_id = lichess_user.get("username")
        if not user_id:
            error_message = "Failed getting user info from Lichess"
            logger.error(error_message)
            return RedirectResponse(f"{FRONTEND_URL}/callback?error={error_message}")

        now = datetime.now(timezone.utc)
        token_expires_at = now + timedelta(hours=1)
        session_expires_at = now + timedelta(
            days=SESSION_DURATION_DAYS
        )  # Long-lived session

        # Generate secure session token
        session_token = generate_session_token(user_id)

        data = {
            "id": user_id,
            "access_token": encrypt_token(access_token),
            "token_expires_at": token_expires_at.isoformat(),
            "session_expires_at": session_expires_at.isoformat(),
            "session_token": session_token,  # Store session token in DB
            "created_at": now.isoformat(),
        }

        try:
            supabase.table("users").upsert(data, on_conflict="id").execute()
        except APIError as e:
            error_message = f"Database error: {e.message}"
            logger.error(error_message)
            return RedirectResponse(f"{FRONTEND_URL}/callback?error={error_message}")

        # Clean up OAuth session data
        request.session.pop("code_verifier", None)

        # Create response with HTTP-only cookie
        response = RedirectResponse(f"{FRONTEND_URL}/callback?success=true", status_code=303)

        # Set HTTP-only cookie for session token (more secure than session storage)
        response.set_cookie(
            key="session_token",
            value=session_token,
            max_age=60 * 60 * 24 * SESSION_DURATION_DAYS,  # 30 days
            httponly=True,  # Prevent XSS attacks
            secure=ENVIRONMENT == "production",  # HTTPS only in production
            samesite="none",
            domain=None,  # Allow cross-origin cookies
        )

        return response

    except Exception as e:
        # Catch any unexpected errors
        error_message = f"Unexpected error during authentication: {str(e)}"
        logger.error(error_message, exc_info=True)
        return RedirectResponse(f"{FRONTEND_URL}/callback?error={error_message}")
'''


@app.post("/api/start_listening")
@limiter.limit("5/minute")
async def start_listening(
    request: Request, request_data: dict, user: dict = Depends(require_auth)
):
    try:
        user_id = user["id"]
        duration = request_data.get("duration", 20)

        # Input validation
        if (
            not isinstance(duration, (int, float))
            or duration <= 0
            or duration > MAX_LISTENING_DURATION
        ):
            raise HTTPException(
                status_code=400,
                detail=f"Duration must be between {MIN_LISTENING_DURATION} and {MAX_LISTENING_DURATION} minutes",
            )

        loop = asyncio.get_running_loop()
        expiry = datetime.now(timezone.utc) + timedelta(minutes=duration)

        # check if user / task not yet in activity queue, else skip
        if user_id not in check_stream_events_queue:
            check_stream_events_queue[user_id] = {
                "expiry": expiry,
                "duration": duration,
                "start_time": datetime.now(timezone.utc),
            }

            # Submit the blocking function to the executor
            executor.submit(listen_for_games_blocking, user, loop)
            # Started listening for user

            return {"success": True, "message": f"Listening for {duration} minutes"}
        else:
            # User already listening
            return {"success": True, "message": "Already listening"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting listening for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to start listening")


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    """Logout user and delete from database"""
    try:
        # Get current user to delete their data
        user = get_current_user(request)
        if user:
            user_id = user["id"]
            
            # Remove from activity queue
            check_stream_events_queue.pop(user_id, None)
            
            # Close WebSocket connection if active
            await manager.close_user_connection(user_id, code=1000, reason="User logged out")
            
            # Revoke Lichess token before deleting user data
            if user.get("access_token"):
                try:
                    decrypted_token = get_user_access_token(user)
                    revoke_result = await revoke_lichess_token(decrypted_token)
                    if revoke_result.get("error"):
                        logger.warning(f"Token revocation warning for {user_id}: {revoke_result['error']}")
                except Exception as e:
                    logger.error(f"Failed to revoke Lichess token for {user_id}: {e}")
            
            # Delete user from database (this removes all tokens and session data)
            try:
                supabase.table("users").delete().eq("id", user_id).execute()
            except Exception as e:
                logger.error(f"Failed to delete user {user_id} from database: {e}")

        return {"success": True, "message": "Logged out successfully"}

    except Exception as e:
        logger.error(f"Logout error: {e}")
        return {"success": True, "message": "Logged out successfully"}


@app.get("/api/auth/status")
async def auth_status(request: Request):
    """Check authentication status"""
    try:
        user = get_current_user(request)
        if user:
            # User authenticated
            return {"authenticated": True, "user_id": user["id"]}
        else:
            # User not authenticated
            return {"authenticated": False}
    except Exception as e:
        logger.error(f"Error checking auth status: {e}")
        return {"authenticated": False, "error": "Authentication check failed"}


# Debug endpoint removed for production security


@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket, token: Optional[str] = None
):
    """WebSocket endpoint for real-time analysis notifications"""
    # Only accept token as query parameter - no fallbacks
    if not token:
        logger.warning("Missing token in WebSocket connection")
        await websocket.close(code=WS_1008_POLICY_VIOLATION)
        return
    
    # Verify session token strictly against DB (signature, expiry, and active DB session)
    token_payload = verify_session_token(token)
    if not token_payload:
        logger.warning("Invalid or revoked token in WebSocket connection")
        await websocket.close(code=WS_1008_POLICY_VIOLATION)
        return

    # Accept the WebSocket connection only if authentication succeeded
    await websocket.accept()

    user_id = token_payload["user_id"]

    await manager.connect(websocket, user_id)
    try:
        while True:
            # Keep the connection alive and handle any incoming messages
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                break
            except Exception as e:
                logger.error(f"WebSocket receive error for user {user_id}: {e}")
                break
    except Exception as e:
        logger.error(f"WebSocket error for user {user_id}: {e}")
    finally:
        manager.disconnect(user_id)


@app.post("/api/cancel_listening")
async def cancel_listening(user: dict = Depends(require_auth)):
    user_id = user["id"]

    # remove from activity queue
    check_stream_events_queue.pop(user_id, None)

    return {"success": True, "message": "Stopped listening"}


@app.get("/api/listening_status")
async def get_listening_status(user: dict = Depends(require_auth)):
    """Get current listening status and remaining time"""
    user_id = user["id"]

    if user_id in check_stream_events_queue:
        queue_data = check_stream_events_queue[user_id]
        expiry_time = queue_data["expiry"]
        duration = queue_data.get("duration", 20)

        # Calculate remaining time
        now = datetime.now(timezone.utc)
        remaining_time = expiry_time - now

        if remaining_time.total_seconds() > 0:
            # Still listening
            return {
                "success": True,
                "listening": True,
                "expiry_time": expiry_time.isoformat(),
                "remaining_seconds": int(remaining_time.total_seconds()),
                "duration": duration,
            }
        else:
            # Expired, remove from queue
            check_stream_events_queue.pop(user_id, None)

    # Not listening
    return {
        "success": True,
        "listening": False,
        "expiry_time": None,
        "remaining_seconds": 0,
        "duration": 0,
    }


@app.post("/api/analyze_user")
@limiter.limit("10/minute")
async def analyze_user(
    request: Request, request_data: dict, user: dict = Depends(require_auth)
):
    """Analyze the current user for both white and black pieces"""
    try:
        user_id = user["id"]
        color = request_data.get("color", "white")  # "white" or "black"
        force_refresh = request_data.get("force_refresh", False)

        # Input validation
        if color not in ["white", "black"]:
            raise HTTPException(
                status_code=400, detail="Color must be 'white' or 'black'"
            )
        if not isinstance(force_refresh, bool):
            raise HTTPException(
                status_code=400, detail="force_refresh must be a boolean"
            )

        # Check if analysis already exists in DB and is recent (within 7 days)
        if not force_refresh:
            try:
                result = (
                    supabase.table("analytics")
                    .select("*")
                    .eq("target_user", user_id)
                    .eq("color", color)
                    .execute()
                )

                if result.data and len(result.data) > 0:
                    analysis_data = result.data[-1]
                    try:
                        # Parse the datetime string and ensure it's timezone-aware
                        created_at_str = analysis_data["created_at"]
                        if created_at_str.endswith("Z"):
                            created_at = datetime.fromisoformat(
                                created_at_str.replace("Z", "+00:00")
                            )
                        else:
                            created_at = datetime.fromisoformat(created_at_str)

                        # Ensure both datetimes are timezone-aware for comparison
                        now = datetime.now(timezone.utc)
                        if created_at.tzinfo is None:
                            # If created_at is naive, assume it's UTC
                            created_at = created_at.replace(tzinfo=timezone.utc)

                        # Use dynamic cache duration based on prep_text content
                        cache_days = get_cache_duration(analysis_data["prep_text"])
                        if created_at > (now - timedelta(days=cache_days)):
                            return {
                                "success": True,
                                "user_id": user_id,
                                "color": color,
                                "preparation": analysis_data["prep_text"],
                                "analyzed_at": analysis_data["created_at"],
                                "cached": True,
                            }
                    except (ValueError, TypeError) as e:
                        logger.warning(
                            f"Error parsing datetime for cached analysis: {e}"
                        )
                        pass  # Error parsing datetime, will generate new analysis
            except APIError as e:
                logger.error(f"Database error checking for cached analysis: {e}")
                # Continue to generate new analysis

        # Get user's access token
        user_access_token = get_user_access_token(user)
        if not user_access_token:
            raise HTTPException(status_code=401, detail="No valid access token")

        # Generate preparation analysis for the user themselves
        prep_text = await generate_prep(user_id, color, user_access_token)

        # create random game_id since no game is active
        game_id = secrets.token_urlsafe(16)

        # Store in Supabase
        data = {
            "game_id": game_id,
            "target_user": user_id,
            "created_by": user_id,
            "color": color,
            "prep_text": prep_text,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            supabase.table("analytics").upsert(data, on_conflict="game_id").execute()
            # Analysis stored successfully
        except APIError as e:
            logger.error(f"Database error storing analysis for {user_id}: {e}")
            # Analysis not stored but we can still return the result

        return {
            "success": True,
            "user_id": user_id,
            "color": color,
            "preparation": prep_text,
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "cached": False,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/user/analysis")
async def get_user_analysis(user: dict = Depends(require_auth)):
    """Get existing user analysis from database for both colors"""
    try:
        user_id = user["id"]

        # Fetch both white and black analysis
        result_white = (
            supabase.table("analytics")
            .select("*")
            .eq("target_user", user_id)
            .eq("color", "white")
            .execute()
        )
        result_black = (
            supabase.table("analytics")
            .select("*")
            .eq("target_user", user_id)
            .eq("color", "black")
            .execute()
        )

        analysis_data = {"white": None, "black": None}

        if result_white.data and len(result_white.data) > 0:
            analysis_data["white"] = result_white.data[-1]

        if result_black.data and len(result_black.data) > 0:
            analysis_data["black"] = result_black.data[-1]

        for color in ["white", "black"]:
            if analysis_data[color] is not None:
                analysis = analysis_data[color]
                try:
                    # Parse the datetime string and ensure it's timezone-aware
                    created_at_str = analysis["created_at"]
                    if created_at_str.endswith("Z"):
                        created_at = datetime.fromisoformat(
                            created_at_str.replace("Z", "+00:00")
                        )
                    else:
                        created_at = datetime.fromisoformat(created_at_str)

                    # Ensure both datetimes are timezone-aware for comparison
                    now = datetime.now(timezone.utc)
                    if created_at.tzinfo is None:
                        # If created_at is naive, assume it's UTC
                        created_at = created_at.replace(tzinfo=timezone.utc)

                    # Use dynamic cache duration based on prep_text content
                    cache_days = get_cache_duration(analysis["prep_text"])
                    if created_at > (now - timedelta(days=cache_days)):
                        analysis_data[color] = {
                            "prep_text": analysis["prep_text"],
                            "analyzed_at": analysis["created_at"],
                            "cached": True,
                        }
                    else:
                        # Analysis is expired, set to null
                        analysis_data[color] = None
                except (ValueError, TypeError):
                    # Error parsing datetime, set to null to force regeneration
                    analysis_data[color] = None

        return {"success": True, "user_id": user_id, "analysis": analysis_data}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/user/analysis/history")
async def get_user_analysis_history(user: dict = Depends(require_auth)):
    """Get all analysis results created by the user (for opponents only, excluding self-analysis)"""
    try:
        user_id = user["id"]

        # Fetch all analysis records where this user is the creator but target_user is different (opponents only)
        result = (
            supabase.table("analytics")
            .select("*")
            .eq("created_by", user_id)
            .neq("target_user", user_id)
            .order("created_at", desc=True)
            .execute()
        )

        analysis_history = []

        if result.data:
            for analysis in result.data:
                analysis_history.append(
                    {
                        "game_id": analysis["game_id"],
                        "opponent": analysis["target_user"],
                        "color": analysis["color"],
                        "preparation": analysis["prep_text"],
                        "created_at": analysis["created_at"],
                        "analyzed_at": analysis["created_at"],  # For compatibility
                    }
                )

        return {
            "success": True,
            "user_id": user_id,
            "analysis_history": analysis_history,
            "total_count": len(analysis_history),
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/public/analysis")
async def get_all_public_analysis():
    """Get all analysis results from the database (public endpoint, no authentication required)"""
    try:
        # Fetch all analysis records from the database, ordered by most recent first
        result = (
            supabase.table("analytics")
            .select("*")
            .order("created_at", desc=True)
            .execute()
        )

        analysis_history = []

        if result.data:
            for analysis in result.data:
                analysis_history.append(
                    {
                        "game_id": analysis["game_id"],
                        "created_by": analysis["created_by"],
                        "opponent": analysis["target_user"],
                        "color": analysis["color"],
                        "preparation": analysis["prep_text"],
                        "created_at": analysis["created_at"],
                        "analyzed_at": analysis["created_at"],  # For compatibility
                    }
                )

        return {
            "success": True,
            "analysis_history": analysis_history,
            "total_count": len(analysis_history),
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.on_event("startup")
async def startup_event():
    """Initialize resources on startup"""
    logger.info("Starting PrepBot-3000 Chess API")
    if ENVIRONMENT == "development":
        logger.info(f"Environment: {ENVIRONMENT}")
        logger.info(f"Port: {PORT}")


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup resources on shutdown"""
    logger.info("Shutting down PrepBot-3000 Chess API")
    await httpx_client.aclose()
