import os
import jwt
import json
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
from slowapi import Limiter, _rate_limit_exceeded_handler
from starlette.middleware.sessions import SessionMiddleware
from fastapi import Cookie, FastAPI, Request, Depends, HTTPException, Response, WebSocket, WebSocketDisconnect
# own modules
from analysis import generate_insights, stream_games_for_user, extract_features
from llm_service import build_context, build_snappy_prompt, generate_llm_recommendations, get_system_prompt

# Constants
MAX_GAMES_ANALYSIS = 50
ANALYSIS_CACHE_DAYS = 7
SESSION_DURATION_DAYS = 30
MIN_LISTENING_DURATION = 1   # minutes
MAX_LISTENING_DURATION = 60  # minutes
WS_1008_POLICY_VIOLATION = 1008

# Configure logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(level=getattr(logging, LOG_LEVEL.upper()))
logger = logging.getLogger(__name__)



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
app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Environment configuration
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
SECRET_KEY = os.getenv("SECRET_KEY")
CLIENT_ID = os.getenv("LICHESS_CLIENT_ID")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000" if ENVIRONMENT == "development" else None)
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000" if ENVIRONMENT == "development" else None)
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
}

missing_vars = [var for var, value in required_vars.items() if not value]
if missing_vars:
    raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")


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
    #raw = user_data.get("access_token") if user_data else None
    #return decrypt_token(raw)
    return user_data.get("access_token")


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
        logger.info(f"WebSocket connected for user: {user_id}")

    def disconnect(self, user_id: str):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
            logger.info(f"WebSocket disconnected for user: {user_id}")

    async def send_personal_message(self, message: dict, user_id: str):
        if user_id in self.active_connections:
            try:
                await self.active_connections[user_id].send_text(json.dumps(message))
                logger.debug(f"Sent WebSocket message to user {user_id}")
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
        logger.info(f"Verifying token: {token[:20]}...")
        logger.info(f"Secret key available: {bool(SECRET_KEY)}")
        
        # Token verification in progress
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        logger.info(f"Decoded payload: {payload}")
        
        if payload.get("type") != "session":
            logger.info("Token type is not 'session'")
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
                    logger.info("Token mismatch in database")
                    return None
            except Exception as e:
                logger.info(f"Token revocation check failed: {e}")
                # Token revocation check failed - continue without revocation check
                pass

        logger.info("Token verification successful")
        return payload
    except jwt.ExpiredSignatureError as e:
        logger.info(f"Token expired: {e}")
        return None
    except jwt.InvalidTokenError as e:
        logger.info(f"Invalid token: {e}")
        return None


def get_current_user(request: Request) -> Optional[dict]:
    """Get current authenticated user from session token"""
    session_token = request.cookies.get("session_token")

    # Debug: Log cookie information
    logger.info(f"Cookies: {dict(request.cookies)}")
    logger.info(f"Session token from cookie: {session_token}")

    if not session_token:
        session_token = request.session.get("session_token")
        logger.info(f"Session token from session: {session_token}")

    if not session_token:
        logger.info("No session token found")
        return None

    logger.info(f"Verifying session token: {session_token[:20]}...")
    token_payload = verify_session_token(session_token)
    if not token_payload:
        logger.info("Session token verification failed")
        return None
    
    logger.info(f"Token payload: {token_payload}")

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
    except Exception:
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

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://lichess.org/api/token",
            json=token_data,
            headers={"Content-Type": "application/json"},
        )
        return response.json()


async def get_lichess_user(access_token: str) -> dict:
    """Get user information from Lichess API"""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://lichess.org/api/account",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return response.json()


def get_lichess_client(user_data: dict) -> berserk.Client:
    """Get authenticated Lichess client using user data from database"""
    access_token = get_user_access_token(user_data)
    if not access_token:
        raise HTTPException(status_code=401, detail="No valid access token")

    session = berserk.TokenSession(access_token)
    return berserk.Client(session=session)


async def generate_prep(opponent_id: str, opponent_color: str, user_access_token: str = None) -> str:
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
        logger.info(f"Starting analysis for opponent: {opponent_id} ({opponent_color})")
        
        if not user_access_token:
            raise ValueError("No user access token provided")
        
        # Step 1: Fetch and analyze games (rate limited)
        max_games = MAX_GAMES_ANALYSIS  # Limit to prevent abuse
        all_feats = []
        
        async for game in stream_games_for_user(opponent_id, user_access_token, opponent_color, max_games=max_games):
            feats = extract_features(game, opponent_color)
            if feats:
                all_feats.append(feats)
        
        if not all_feats:
            return f"⚠️ No recent games found for {opponent_id}. Cannot generate preparation analysis."
        
        df = pd.DataFrame(all_feats)
        logger.info(f"Analyzed {len(df)} games for {opponent_id}")
        
        # Step 2: Generate insights 
        insights = generate_insights(df, min_info_gain=0.0, top_k=5)


        if not insights:
            raise ValueError("No insights generated")

        # Step 3: Build context and prompts
        context = build_context(insights, opponent_id, opponent_color, len(df))
        prompt = build_snappy_prompt(context)
        system_prompt = get_system_prompt()

        # Step 4: generate recommendations
        quick_analysis = generate_llm_recommendations(system_prompt=system_prompt, prompt=prompt, groq_api_key=GROQ_API_KEY)
        
        if not quick_analysis:
            raise ValueError("No recommendations generated")
        
        return quick_analysis
        

    except Exception as e:
        logger.error(f"Error generating prep for {opponent_id}: {e}")
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
                    handle_game_start_event(game_id, user_id, opponent_id, opponent_color, user_access_token), loop
                )
                # You could optionally wait for the result, but it's not necessary here
                # future.result()


    except Exception:
        # cancel activity from queue
        check_stream_events_queue.pop(user_id, None)
        # Raising HTTPException here will crash the thread, not send a response.
        # It's better to just log the error.



async def handle_game_start_event(game_id: str, user_id: str, opponent_id: str, opponent_color: str, user_access_token: str = None):
    """Asynchronous handler for game start events."""


    # Check DB if a recent analysis already exists
    #result = supabase.table("analytics").select("*").eq("target_user", opponent_id).execute()
    result = supabase.table("analytics").select("*").eq("target_user", opponent_id).eq("color", opponent_color).execute()

    if result.data and len(result.data) > 0:
        user_data = result.data[0]
        try:
            # Parse the datetime string and ensure it's timezone-aware
            created_at_str = user_data["created_at"]
            if created_at_str.endswith('Z'):
                created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
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
            "message": f"New analysis completed for {opponent_id} ({opponent_color})"
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

    # Store verifier in temporary storage with a unique key
    import uuid
    state_key = str(uuid.uuid4())
    oauth_verifiers[state_key] = verifier
    
    # Debug: Log storage
    logger.info(f"Stored verifier with key: {state_key[:10]}...")
    logger.info(f"Verifier: {verifier[:10]}...")

    oauth_params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": f"{base_url}/auth/callback",
        "state": state_key,  # Include state parameter
        # "scope": "preference:read stream:watch",
        "scope": "preference:read challenge:read",
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }

    oauth_url = f"https://lichess.org/oauth?{urlencode(oauth_params)}"
    return RedirectResponse(oauth_url)


@app.get("/auth/callback")
async def callback(request: Request, code: str = None, state: str = None):
    """Handle OAuth callback from Lichess"""
    if not code:
        raise HTTPException(status_code=400, detail="No authorization code provided")

    if not state:
        raise HTTPException(status_code=400, detail="No state parameter provided")

    # Debug: Log state and verifier lookup
    logger.info(f"Received state: {state}")
    logger.info(f"Available verifier keys: {list(oauth_verifiers.keys())}")
    
    verifier = oauth_verifiers.get(state)
    if not verifier:
        logger.error(f"No code verifier found for state: {state}")
        raise HTTPException(status_code=400, detail="No code verifier found in session")
    
    # Clean up the verifier from temporary storage
    oauth_verifiers.pop(state, None)
    logger.info(f"Retrieved and cleaned up verifier for state: {state}")

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
    session_expires_at = now + timedelta(days=SESSION_DURATION_DAYS)  # Long-lived session

    # Generate secure session token
    session_token = generate_session_token(user_id)
    logger.info(f"Generated session token: {session_token[:20]}...")

    data = {
        "id": user_id,
        #"access_token": encrypt_token(access_token),
        "access_token": access_token,
        "token_expires_at": token_expires_at.isoformat(),
        "session_expires_at": session_expires_at.isoformat(),
        "session_token": session_token,  # Store session token in DB
        "created_at": now.isoformat(),
    }

    try:
        supabase.table("users").upsert(data, on_conflict="id").execute()
        logger.info(f"User data stored in database for user: {user_id}")
    except APIError as e:
        logger.error(f"Database error: {e.message}")
        raise HTTPException(status_code=500, detail=f"Supabase error: {e.message}")

    # OAuth verifier already cleaned up from temporary storage

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
    
    logger.info(f"Set session_token cookie: {session_token[:20]}...")
    logger.info(f"Cookie settings: httponly=True, secure={ENVIRONMENT == 'production'}, samesite=lax")

    return response
    # return {"message": f"Logged in as {user_id}, lichess token: {access_token}"}




@app.post("/api/start_listening")
@limiter.limit("5/minute")
async def start_listening(request: Request, request_data: dict, user: dict = Depends(require_auth)):
    user_id = user["id"]
    duration = request_data.get("duration", 20)
    
    # Input validation
    if not isinstance(duration, (int, float)) or duration <= 0 or duration > MAX_LISTENING_DURATION:
        raise HTTPException(status_code=400, detail=f"Duration must be between {MIN_LISTENING_DURATION} and {MAX_LISTENING_DURATION} minutes")
    loop = asyncio.get_running_loop()
    expiry = datetime.now(timezone.utc) + timedelta(minutes=duration)

    # check if user / task not yet in activity queue, else skip
    if user_id not in check_stream_events_queue:
        check_stream_events_queue[user_id] = {
            "expiry": expiry,
            "duration": duration,
            "start_time": datetime.now(timezone.utc)
        }

        # Submit the blocking function to the executor
        executor.submit(listen_for_games_blocking, user, loop)

        return {"success": True, "message": f"Listening for {duration} minutes"}
    else:
        return {"success": True, "message": "Already listening"}


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
    logger.info("Checking auth status...")
    user = get_current_user(request)
    if user:
        logger.info(f"User authenticated: {user['id']}")
        return {"authenticated": True, "user_id": user["id"]}
    else:
        logger.info("User not authenticated")
        return {"authenticated": False}


# Debug endpoint removed for production security


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, session_token: Optional[str] = Cookie(None)):
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
            #data = await websocket.receive_text()
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(user_id)
    except Exception as e:
        logger.error(f"WebSocket error for user {user_id}: {e}")
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
                "duration": duration
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
        "duration": 0
    }

@app.post("/api/analyze_user")
@limiter.limit("10/minute")
async def analyze_user(request: Request, request_data: dict, user: dict = Depends(require_auth)):
    """Analyze the current user for both white and black pieces"""
    try:
        user_id = user["id"]
        color = request_data.get("color", "white")  # "white" or "black"
        force_refresh = request_data.get("force_refresh", False)
        
        # Input validation
        if color not in ["white", "black"]:
            raise HTTPException(status_code=400, detail="Color must be 'white' or 'black'")
        if not isinstance(force_refresh, bool):
            raise HTTPException(status_code=400, detail="force_refresh must be a boolean")
        
        # Check if analysis already exists in DB and is recent (within 7 days)
        if not force_refresh:
            result = supabase.table("analytics").select("*").eq("target_user", user_id).eq("color", color).execute()
            
            if result.data and len(result.data) > 0:
                analysis_data = result.data[0]
                try:
                    # Parse the datetime string and ensure it's timezone-aware
                    created_at_str = analysis_data["created_at"]
                    if created_at_str.endswith('Z'):
                        created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
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
                            "cached": True
                        }
                except (ValueError, TypeError):
                    pass  # Error parsing datetime, will generate new analysis
        
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
        except APIError:
            # Database error - analysis not stored
            pass
        
        return {
            "success": True,
            "user_id": user_id,
            "color": color,
            "preparation": prep_text,
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "cached": False
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/user/analysis")
async def get_user_analysis(user: dict = Depends(require_auth)):
    """Get existing user analysis from database for both colors"""
    try:
        user_id = user["id"]
        
        # Fetch both white and black analysis
        result = supabase.table("analytics").select("*").eq("target_user", user_id).execute()
        
        analysis_data = {
            "white": None,
            "black": None
        }
        
        if result.data:
            for analysis in result.data:
                color = analysis["color"]
                if color in analysis_data:
                    # Check if analysis is recent (within 7 days)
                    try:
                        # Parse the datetime string and ensure it's timezone-aware
                        created_at_str = analysis["created_at"]
                        if created_at_str.endswith('Z'):
                            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
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
                                "cached": True
                            }
                    except (ValueError, TypeError):
                        continue
        
        return {
            "success": True,
            "user_id": user_id,
            "analysis": analysis_data
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/user/analysis/history")
async def get_user_analysis_history(user: dict = Depends(require_auth)):
    """Get all analysis results created by the user (for opponents only, excluding self-analysis)"""
    try:
        user_id = user["id"]
        
        # Fetch all analysis records where this user is the creator but target_user is different (opponents only)
        result = supabase.table("analytics").select("*").eq("created_by", user_id).neq("target_user", user_id).order("created_at", desc=True).execute()
        
        analysis_history = []
        
        if result.data:
            for analysis in result.data:
                analysis_history.append({
                    "game_id": analysis["game_id"],
                    "opponent": analysis["target_user"],
                    "color": analysis["color"],
                    "preparation": analysis["prep_text"],
                    "created_at": analysis["created_at"],
                    "analyzed_at": analysis["created_at"]  # For compatibility
                })
        
        return {
            "success": True,
            "user_id": user_id,
            "analysis_history": analysis_history,
            "total_count": len(analysis_history)
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/public/analysis")
async def get_all_public_analysis():
    """Get all analysis results from the database (public endpoint, no authentication required)"""
    try:
        # Fetch all analysis records from the database, ordered by most recent first
        result = supabase.table("analytics").select("*").order("created_at", desc=True).execute()
        
        analysis_history = []
        
        if result.data:
            for analysis in result.data:
                analysis_history.append({
                    "game_id": analysis["game_id"],
                    "created_by": analysis["created_by"],
                    "opponent": analysis["target_user"],
                    "color": analysis["color"],
                    "preparation": analysis["prep_text"],
                    "created_at": analysis["created_at"],
                    "analyzed_at": analysis["created_at"]  # For compatibility
                })
        
        return {
            "success": True,
            "analysis_history": analysis_history,
            "total_count": len(analysis_history)
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
