// Shared configuration and helpers
window.API_BASE_URL = window.API_BASE_URL || window.location.origin.replace(':3000', ':8000');

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

// Set the login button href dynamically (or swap CTA if logged in)
function initializeLoginButton() {
    const loginBtn = document.getElementById('login-button');
    const ctaContainer = document.getElementById('primary-cta');
    if (loginBtn) loginBtn.href = `${window.API_BASE_URL}/auth/login`;

    // Check if the user is logged in and show dashboard instead of login page if user is logged in
    async function checkLogin() {
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
          // User is logged in: swap primary CTA to Dashboard
          if (loginBtn) {
            loginBtn.textContent = 'Go to Dashboard';
            loginBtn.className = 'btn btn--success';
            loginBtn.href = '/dashboard';
          }
        }
      }
      catch (error) {
        // If there's an error, assume not logged in and do nothing.
        // The user stays on the login page.
        console.error('Auth check failed:', error);
      }
      // Note: CTA is now visible by default, no need to show it here
      }

      // Run checkLogin asynchronously without blocking page load
      setTimeout(checkLogin, 100);
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