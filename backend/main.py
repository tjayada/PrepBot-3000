from fastapi import FastAPI, Request, Depends, HTTPException, Response
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
import httpx
import hashlib
import base64
import secrets
import dotenv
import os
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone
from postgrest import APIError
from supabase import create_client, Client
import berserk
import jwt
from typing import Optional


############### Setup ###############

dotenv.load_dotenv()
app = FastAPI()

SECRET_KEY = os.getenv("SECRET_KEY")
CLIENT_ID = os.getenv("LICHESS_CLIENT_ID")
BASE_URL = os.getenv(
    "BASE_URL", "http://localhost:8000"
)  # Default for my development for now
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

if not SECRET_KEY:
    raise ValueError("SECRET_KEY environment variable is required")
if not CLIENT_ID:
    raise ValueError("LICHESS_CLIENT_ID environment variable is required")

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
    https_only=False,  # Set to False for local development
)

PORT = int(os.getenv("PORT", 8000))


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
        print(
            f"DEBUG: Verifying token with SECRET_KEY: {SECRET_KEY[:10] if SECRET_KEY else None}..."
        )
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        print(f"DEBUG: Token decoded successfully: {payload}")

        if payload.get("type") != "session":
            print(
                f"DEBUG: Token type mismatch. Expected 'session', got '{payload.get('type')}'"
            )
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
                    print("DEBUG: Token revoked - database token differs")
                    return None
            except Exception as e:
                print(f"DEBUG: Error checking token revocation: {e}")

        return payload
    except jwt.ExpiredSignatureError as e:
        print(f"DEBUG: Token expired: {e}")
        return None
    except jwt.InvalidTokenError as e:
        print(f"DEBUG: Invalid token: {e}")
        return None


def get_current_user(request: Request) -> Optional[dict]:
    """Get current authenticated user from session token"""
    print("=== DEBUG: get_current_user called ===")
    print(f"All cookies: {dict(request.cookies)}")
    print(f"Session data: {dict(request.session)}")

    session_token = request.cookies.get("session_token")
    print(
        f"Session token from cookie: {session_token[:50] if session_token else None}..."
    )

    if not session_token:
        session_token = request.session.get("session_token")
        print(
            f"Session token from session: {session_token[:50] if session_token else None}..."
        )

    if not session_token:
        print("DEBUG: No session token found")
        return None

    print("DEBUG: Verifying session token...")
    token_payload = verify_session_token(session_token)
    if not token_payload:
        print("DEBUG: Token verification failed")
        return None

    print(f"DEBUG: Token payload: {token_payload}")

    # Get user data from database
    try:
        result = (
            supabase.table("users")
            .select("*")
            .eq("id", token_payload["user_id"])
            .execute()
        )
        print(f"DEBUG: Database query result: {result.data}")

        if result.data and len(result.data) > 0:
            user_data = result.data[0]
            print(f"DEBUG: User data from DB: {user_data}")

            if user_data.get("session_token") != session_token:
                print("DEBUG: Token revoked - database token differs")
                return None

            # Check if session hasn't expired in database
            if user_data.get("session_expires_at"):
                expires_at = datetime.fromisoformat(
                    user_data["session_expires_at"].replace("Z", "+00:00")
                )
                now = datetime.now(timezone.utc)
                print(f"DEBUG: Session expires at: {expires_at}, Current time: {now}")
                if expires_at < now:
                    print("DEBUG: Session expired")
                    return None
            print("DEBUG: User authenticated successfully")
            return user_data
        else:
            print("DEBUG: No user data found in database")
    except Exception as e:
        print(f"DEBUG: Error fetching user data: {e}")

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
    access_token = user_data.get("access_token")
    if not access_token:
        raise HTTPException(status_code=401, detail="No valid access token")

    session = berserk.TokenSession(access_token)
    return berserk.Client(session=session)


############### Define API Endpoints ###############


@app.get("/auth/login")
async def login(request: Request):
    """Initiate OAuth login with Lichess"""
    base_url = (
        BASE_URL
        if BASE_URL != "http://localhost:8000"
        else f"{request.url.scheme}://{request.url.netloc}"
    )

    verifier = create_verifier()
    challenge = create_challenge(verifier)

    request.session["code_verifier"] = verifier

    oauth_params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": f"{base_url}/auth/callback",
        # "scope": "preference:read stream:watch",
        "scope": "preference:read",
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

    base_url = (
        BASE_URL
        if BASE_URL != "http://localhost:8000"
        else f"{request.url.scheme}://{request.url.netloc}"
    )
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
    session_expires_at = now + timedelta(days=30)  # Long-lived session

    # Generate secure session token
    session_token = generate_session_token(user_id)
    print(f"DEBUG: Generated session token: {session_token[:50]}...")

    data = {
        "id": user_id,
        "access_token": access_token,
        "token_expires_at": token_expires_at.isoformat(),
        "session_expires_at": session_expires_at.isoformat(),
        "session_token": session_token,  # Store session token in DB
        "created_at": now.isoformat(),
    }
    print(f"DEBUG: User data to store: {data}")

    try:
        result = supabase.table("users").upsert(data, on_conflict="id").execute()
        print(f"DEBUG: Database upsert result: {result.data}")
    except APIError as e:
        print(f"DEBUG: Database error: {e}")
        raise HTTPException(status_code=500, detail=f"Supabase error: {e.message}")

    # Clean up OAuth session data
    request.session.pop("code_verifier", None)

    print(f"DEBUG: Generated session token for user: {user_id}")  # Debug print

    # Create response with HTTP-only cookie
    response = RedirectResponse(f"{FRONTEND_URL}/dashboard", status_code=303)

    # Set HTTP-only cookie for session token (more secure than session storage)
    print(f"DEBUG: Setting cookie with session token: {session_token[:50]}...")
    response.set_cookie(
        key="session_token",
        value=session_token,
        max_age=60 * 60 * 24 * 30,  # 30 days
        httponly=True,  # Prevent XSS attacks
        secure=False,  # Set to True in production with HTTPS
        samesite="lax",  # CSRF protection
        domain=None,  # Allow cross-origin cookies
    )
    print("DEBUG: Cookie set successfully, redirecting to dashboard")

    return response
    # return {"message": f"Logged in as {user_id}, lichess token: {access_token}"}


@app.get("/api/user/stats")
async def get_user_stats(user: dict = Depends(require_auth)):
    """Get user stats - requires authentication"""
    try:
        print(f"Authenticated user: {user['id']}")

        client = get_lichess_client(user)

        # Fetch from Lichess API using authenticated user's data
        lichess_data = client.users.get_public_data(user["id"])

        # Return structured JSON
        return {
            "success": True,
            "data": {
                "username": lichess_data["username"],
                "rating": {
                    "blitz": lichess_data["perfs"]["blitz"]["rating"],
                    "rapid": lichess_data["perfs"]["rapid"]["rating"],
                    "bullet": lichess_data["perfs"]["bullet"]["rating"],
                },
                "games_played": lichess_data["count"]["all"],
                "wins": lichess_data["count"]["win"],
            },
        }
    except Exception as e:
        print(f"Error fetching user stats: {e}")
        return {"success": False, "error": "Failed to fetch user stats"}


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    """Logout user and invalidate session"""
    try:
        # Get current user to invalidate their session
        user = get_current_user(request)
        if user:
            # Invalidate session in database by setting expired time AND clearing token
            now = datetime.now(timezone.utc)
            supabase.table("users").update(
                {
                    "session_expires_at": now.isoformat(),
                    "session_token": None,  # Revoke the token
                }
            ).eq("id", user["id"]).execute()

            print(f"Logged out user: {user['id']}")

        # Clear the HTTP-only cookie
        response.delete_cookie(
            key="session_token",
            httponly=True,
            secure=False,  # Set to True in production with HTTPS
            samesite="lax",
            domain=None,
        )

        return {"success": True, "message": "Logged out successfully"}

    except Exception as e:
        print(f"Logout error: {e}")
        # Still clear the cookie even if DB update fails
        response.delete_cookie(
            key="session_token",
            httponly=True,
            secure=False,
            samesite="lax",
            domain=None,
        )
        return {"success": True, "message": "Logged out successfully"}


@app.get("/api/auth/status")
async def auth_status(request: Request):
    """Check authentication status"""
    user = get_current_user(request)
    if user:
        return {"authenticated": True, "user_id": user["id"]}
    else:
        return {"authenticated": False}


@app.get("/api/debug/cookies")
async def debug_cookies(request: Request):
    """Debug endpoint to check cookies and session data"""
    return {
        "cookies": dict(request.cookies),
        "session": dict(request.session),
        "headers": dict(request.headers),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
