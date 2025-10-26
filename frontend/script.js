// Shared configuration and helpers
// Set API base URL based on environment
window.API_BASE_URL = window.location.hostname === 'localhost' 
  ? 'http://localhost:8000'  // Development
  : 'https://prepbot-3000.onrender.com';  // Production - update this after deployment
// window.API_BASE_URL = window.API_BASE_URL || window.location.origin.replace(':3000', ':8000');

window.logout = async function() {
    try {
        await fetch(`${window.API_BASE_URL}/api/logout`, {
            method: 'POST',
            credentials: 'include'
        });
    } catch (error) {
        console.log('Logout request failed, but redirecting anyway');
    }
    window.location.href = '/';
};

window.loginWithLichess = async function() {
    try {
        // Show loading state
        const ctaContainer = document.getElementById('primary-cta');
        if (ctaContainer) {
            ctaContainer.innerHTML = '<p>Redirecting to Lichess...</p>';
        }

        // Get OAuth URL from backend
        const response = await fetch(`${window.API_BASE_URL}/api/auth/oauth-url`, {
            method: 'GET',
            credentials: 'include',
            headers: {
                'Content-Type': 'application/json'
            }
        });

        if (!response.ok) {
            throw new Error('Failed to get OAuth URL');
        }

        const data = await response.json();
        
        if (data.oauth_url) {
            // Redirect to Lichess OAuth
            window.location.href = data.oauth_url;
        } else {
            throw new Error('No OAuth URL received');
        }

    } catch (error) {
        console.error('Login error:', error);
        // Reset UI on error
        const ctaContainer = document.getElementById('primary-cta');
        if (ctaContainer) {
            ctaContainer.innerHTML = '';
            initializeLoginButton();
        }
        alert('Login failed: ' + error.message);
    }
};

const chessboard = document.getElementById('chessboard');

// Scroll-aware header background
function updateHeaderScrolledState() {
    const headers = document.querySelectorAll('.header');
    const scrolled = window.scrollY > 8;
    headers.forEach(h => {
        if (scrolled) {
            h.classList.add('scrolled');
        } else {
            h.classList.remove('scrolled');
        }
    });
}

window.addEventListener('scroll', updateHeaderScrolledState, { passive: true });
document.addEventListener('DOMContentLoaded', updateHeaderScrolledState);
let lastInteraction = Date.now();
const inactivityTime = 5000; // 5 seconds inactivity
let animationFrameId = null;

// Create chessboard squares
function createChessboard() {
    const squareSize = 250;
    const cols = Math.ceil(window.innerWidth / squareSize);
    const rows = Math.ceil(window.innerHeight / squareSize);

    chessboard.style.gridTemplateColumns = `repeat(${cols}, ${squareSize}px)`;
    chessboard.style.gridTemplateRows = `repeat(${rows}, ${squareSize}px)`;

    // Clear existing squares
    chessboard.innerHTML = '';

    // Create grid of divs
    for (let y = 0; y < rows; y++) {
        for (let x = 0; x < cols; x++) {
            const square = document.createElement('div');
            square.classList.add('square');
            
            const randomDuration = Math.floor(Math.random() * 10) + 2;
            square.dataset.longTransition = `opacity ${randomDuration}s ease-in`;
            
            // White squares on alternating positions
            if ((x + y) % 2 === 0) {
                square.classList.add('white');
            }
            chessboard.appendChild(square);
        }
    }
}

// Check for inactivity and toggle visibility
function checkInactivity() {
    const now = Date.now();
    if (now - lastInteraction > inactivityTime) {
        document.querySelectorAll('.square.white').forEach(square => {
            square.style.transition = square.dataset.longTransition;
            square.classList.add('visible');
        });
    }
    animationFrameId = requestAnimationFrame(checkInactivity);
}

// Reset on user interaction
function resetAnimation() {
    lastInteraction = Date.now();
    document.querySelectorAll('.square').forEach(square => {
        square.style.transition = '';
        square.classList.remove('visible');
    });
    if (animationFrameId) {
        cancelAnimationFrame(animationFrameId);
    }
    animationFrameId = requestAnimationFrame(checkInactivity);
}

// Event listeners for user interaction
window.addEventListener('mousemove', resetAnimation);
window.addEventListener('touchstart', resetAnimation);
window.addEventListener('keydown', resetAnimation);
window.addEventListener('resize', () => {
    createChessboard();
    resetAnimation();
});

// Initialize
createChessboard();
checkInactivity(); 

// Initialize login state and display appropriate buttons
async function initializeLoginButton() {
  const ctaContainer = document.getElementById('primary-cta');
  //const tableButtons = document.querySelector('.table-btns');
  
  try {
    const response = await fetch(`${window.API_BASE_URL}/api/auth/status`, {
      method: 'GET',
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json'
      }
    });
    
    const data = await response.json();
    
    if (data.authenticated) {
      // User is logged in - show dashboard/logout/revoke buttons
      if (ctaContainer) {
        const dashboardBtn = createButton(
            'Dashboard', 
            'btn--success', 
            () => window.location.href = '/dashboard'
        );
        
        const publicTableBtn = createButton(
            'Public Database', 
            'btn--primary', 
            () => window.location.href = '/public_table'
        );

        // Add logout and revoke buttons after the public table button
        const logoutRevokeBtn = createButton(
            'Logout & Revoke Access', 
            'btn--danger', 
            logout
        );
        
        ctaContainer.appendChild(dashboardBtn);
        ctaContainer.appendChild(publicTableBtn);
        ctaContainer.appendChild(logoutRevokeBtn);
      }

    }
    else {
      // User is not logged in - show login button
      if (ctaContainer) {
        const loginBtn = createButton(
            'Login with Lichess', 
            'btn--success', 
            loginWithLichess
        );
        
        const publicTableBtn = createButton(
            'Public Database', 
            'btn--primary', 
            () => window.location.href = '/public_table'
        );
        
        ctaContainer.appendChild(loginBtn);
        ctaContainer.appendChild(publicTableBtn);
      }
    }
    
  } catch (error) {
    console.error('Auth check failed:', error);
    // Leave default UI (login button) on error
  }
}

// Helper function to create button elements
function createButton(text, className, onClick) {
  const btn = document.createElement('a');
  btn.textContent = text;
  btn.className = `btn ${className}`;
  btn.onclick = onClick;
  return btn;
}

// Robo image toggle functionality
function initializeRoboImage() {
    const roboImage = document.getElementById('robo-image');
    const toggleBtn = document.getElementById('robo-toggle-btn');
    
    // Randomly choose initial image (1 or 2)
    const initialImageNumber = Math.random() < 0.5 ? 1 : 2;
    let isRobo1 = initialImageNumber === 1;
    
    // Set initial random image
    if (roboImage) {
      roboImage.src = `/assets/images/robo-${initialImageNumber}.png`;
    }
    
    if (toggleBtn && roboImage) {
      toggleBtn.addEventListener('click', function() {
        if (isRobo1) {
          roboImage.src = '/assets/images/robo-2.png';
          isRobo1 = false;
        } else {
          roboImage.src = '/assets/images/robo-1.png';
          isRobo1 = true;
        }
      });
    }
}

// Initialize all functionality when DOM is loaded
document.addEventListener('DOMContentLoaded', function() {
    initializeLoginButton();
    initializeRoboImage();
});