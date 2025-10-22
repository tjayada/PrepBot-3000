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
from starlette.middleware.sessions import SessionMiddleware
from fastapi import (
    Cookie,
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
SESSION_DURATION_DAYS = 30
MIN_LISTENING_DURATION = 1  # minutes
MAX_LISTENING_DURATION = 60  # minutes
WS_1008_POLICY_VIOLATION = 1008

# Environment configuration (needed for logging setup)
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

# Configure logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "WARNING")
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# Reduce logging verbosity in production
if ENVIRONMENT == "production":
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


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

        # Add request ID to logger context (only log in development)
        if ENVIRONMENT == "development":
            logger.debug(f"Request {request_id}: {request.method} {request.url.path}")

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
    # return user_data.get("access_token")


# Add CORS middleware for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add session middleware
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    max_age=60 * 60 * 24 * 30,  # 30 days
    same_site="lax",
    https_only=ENVIRONMENT == "production",  # HTTPS only in production
)

PORT = int(os.getenv("PORT", 8000))

# Configure httpx client with connection pooling
httpx_client = httpx.AsyncClient(
    timeout=httpx.Timeout(30.0),
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
    http2=True,
)

executor = ThreadPoolExecutor(max_workers=10)
check_stream_events_queue = {}

# Temporary storage for OAuth verifiers (in production, use Redis or database)
oauth_verifiers = {}


# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, user_id: str):
        self.active_connections[user_id] = websocket
        # WebSocket connected

    def disconnect(self, user_id: str):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
            # WebSocket disconnected

    async def send_personal_message(self, message: dict, user_id: str):
        if user_id in self.active_connections:
            try:
                await self.active_connections[user_id].send_text(json.dumps(message))
                # WebSocket message sent
            except Exception as e:
                logger.error(f"Error sending WebSocket message to {user_id}: {e}")
                self.disconnect(user_id)


manager = ConnectionManager()

############### Define Functions ###############


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


def verify_session_token(token: str, user_id: str = None) -> Optional[dict]:
    """Verify and decode session token"""
    try:
        # Token verification in progress

        # Token verification in progress
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])

        if payload.get("type") != "session":
            logger.warning("Invalid token type")
            return None

        if user_id:
            try:
                result = (
                    supabase.table("users")
                    .select("session_token")
                    .eq("id", user_id)
                    .execute()
                )
                if result.data and result.data[0].get("session_token") != token:
                    logger.warning("Token mismatch in database")
                    return None
            except Exception as e:
                logger.error(f"Token revocation check failed: {e}")
                # Token revocation check failed - continue without revocation check
                pass

        # Token verification successful
        return payload
    except jwt.ExpiredSignatureError:
        # Token expired
        return None
    except jwt.InvalidTokenError:
        # Invalid token
        return None


def get_current_user(request: Request) -> Optional[dict]:
    """Get current authenticated user from session token"""
    session_token = request.cookies.get("session_token")

    if not session_token:
        session_token = request.session.get("session_token")

    if not session_token:
        # No session token found
        return None

    # Verifying session token
    token_payload = verify_session_token(session_token)
    if not token_payload:
        # Session token verification failed
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
                    # Session expired
                    return None
            return user_data
    except Exception as e:
        logger.error(f"Error fetching user data: {e}")
        # Error fetching user data - return None for security
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
        .eq("target_user", opponent_id)
        .eq("color", opponent_color)
        .execute()
    )

    if result.data and len(result.data) > 0:
        user_data = result.data[0]
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

            if created_at > now - timedelta(days=ANALYSIS_CACHE_DAYS):
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

        # Send WebSocket notification to the user about new analysis
        notification = {
            "type": "new_analysis",
            "opponent": opponent_id,
            "color": opponent_color,
            "game_id": game_id,
            "message": f"New analysis completed for {opponent_id} ({opponent_color})",
        }
        await manager.send_personal_message(notification, user_id)

    except APIError:
        # Database error - analysis not stored
        pass


############### Define API Endpoints ###############


@app.get("/health")
async def health_check():
    """Health check endpoint for deployment monitoring"""
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/auth/login")
async def login(request: Request):
    """Initiate OAuth login with Lichess"""
    base_url = BASE_URL

    verifier = create_verifier()
    challenge = create_challenge(verifier)

    request.session["code_verifier"] = verifier

    oauth_params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": f"{base_url}/auth/callback",
        "scope": "preference:read challenge:read",
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }

    oauth_url = f"https://lichess.org/oauth?{urlencode(oauth_params)}"
    return RedirectResponse(oauth_url)


@app.get("/auth/callback")
async def callback(request: Request, code: str = None):
    """Handle OAuth callback from Lichess"""
    if not code:
        raise HTTPException(status_code=400, detail="No authorization code provided")

    verifier = request.session.get("code_verifier")
    if not verifier:
        raise HTTPException(status_code=400, detail="No code verifier found in session")

    base_url = BASE_URL
    redirect_uri = f"{base_url}/auth/callback"

    lichess_token = await get_lichess_token(code, verifier, redirect_uri)

    if not lichess_token.get("access_token"):
        raise HTTPException(status_code=400, detail="Failed getting token")

    access_token = lichess_token["access_token"]
    lichess_user = await get_lichess_user(access_token)
    user_id = lichess_user.get("username")
    if not user_id:
        raise HTTPException(status_code=400, detail="Failed getting user info")

    now = datetime.now(timezone.utc)
    token_expires_at = now + timedelta(hours=1)
    session_expires_at = now + timedelta(
        days=SESSION_DURATION_DAYS
    )  # Long-lived session

    # Generate secure session token
    session_token = generate_session_token(user_id)

    data = {
        "id": user_id,
        "access_token": access_token,
        "token_expires_at": token_expires_at.isoformat(),
        "session_expires_at": session_expires_at.isoformat(),
        "session_token": session_token,  # Store session token in DB
        "created_at": now.isoformat(),
    }

    try:
        supabase.table("users").upsert(data, on_conflict="id").execute()
    except APIError as e:
        raise HTTPException(status_code=500, detail=f"Supabase error: {e.message}")

    # Clean up OAuth session data
    request.session.pop("code_verifier", None)

    # Create response with HTTP-only cookie
    response = RedirectResponse(f"{FRONTEND_URL}/dashboard", status_code=303)

    # Set HTTP-only cookie for session token (more secure than session storage)
    response.set_cookie(
        key="session_token",
        value=session_token,
        max_age=60 * 60 * 24 * SESSION_DURATION_DAYS,  # 30 days
        httponly=True,  # Prevent XSS attacks
        secure=ENVIRONMENT == "production",  # HTTPS only in production
        samesite="lax",  # CSRF protection
        domain=None,  # Allow cross-origin cookies
    )

    return response


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
    """Logout user and invalidate session"""
    try:
        # Get current user to invalidate their session
        user = get_current_user(request)
        if user:
            # remove from activity queue
            check_stream_events_queue.pop(user["id"], None)
            # Invalidate session in database by setting expired time AND clearing token
            now = datetime.now(timezone.utc)
            supabase.table("users").update(
                {
                    "session_expires_at": now.isoformat(),
                    "session_token": None,  # Revoke the token
                }
            ).eq("id", user["id"]).execute()

        # Clear the HTTP-only cookie
        response.delete_cookie(
            key="session_token",
            httponly=True,
            secure=ENVIRONMENT == "production",  # HTTPS only in production
            samesite="lax",
            domain=None,
        )

        return {"success": True, "message": "Logged out successfully"}

    except Exception:
        # Still clear the cookie even if DB update fails
        response.delete_cookie(
            key="session_token",
            httponly=True,
            secure=ENVIRONMENT == "production",  # HTTPS only in production
            samesite="lax",
            domain=None,
        )
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
    websocket: WebSocket, session_token: Optional[str] = Cookie(None)
):
    """WebSocket endpoint for real-time analysis notifications"""
    # Verify the user is authenticated by checking their session token
    user_data = verify_session_token(session_token)
    if not user_data:
        # Reject the connection if authentication fails
        await websocket.close(code=WS_1008_POLICY_VIOLATION)
        return

    # Accept the WebSocket connection only if authentication succeeded
    await websocket.accept()

    user_id = user_data["user_id"]

    await manager.connect(websocket, user_id)
    try:
        while True:
            # Keep the connection alive and handle any incoming messages
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                # WebSocket disconnected
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
                    analysis_data = result.data[0]
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

                        if created_at > now - timedelta(days=ANALYSIS_CACHE_DAYS):
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
        result = (
            supabase.table("analytics").select("*").eq("target_user", user_id).execute()
        )

        analysis_data = {"white": None, "black": None}

        if result.data:
            for analysis in result.data:
                color = analysis["color"]
                if color in analysis_data:
                    # Check if analysis is recent (within 7 days)
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

                        if created_at > now - timedelta(days=ANALYSIS_CACHE_DAYS):
                            analysis_data[color] = {
                                "preparation": analysis["prep_text"],
                                "analyzed_at": analysis["created_at"],
                                "cached": True,
                            }
                    except (ValueError, TypeError):
                        continue

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
    logger.info("Starting Lichess Scout API")
    if ENVIRONMENT == "development":
        logger.info(f"Environment: {ENVIRONMENT}")
        logger.info(f"Port: {PORT}")


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup resources on shutdown"""
    logger.info("Shutting down Lichess Scout API")
    await httpx_client.aclose()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info" if ENVIRONMENT == "production" else "debug",
        access_log=ENVIRONMENT == "development",
    )
