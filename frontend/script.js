const chessboard = document.getElementById('chessboard');
let lastInteraction = Date.now();
const inactivityTime = 3000; // 3 seconds inactivity
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
            
            // Old solution, might delete later if no bugs come up again with current solution above
            //const fixedDuration = 2; // Consistent fade-in duration to prevent rendering artifacts
            //const randomDelay = Math.random() * 1.5; // Stagger the start time of the animation
            //square.dataset.longTransition = `opacity ${fixedDuration}s ease-in ${randomDelay}s`;
            
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