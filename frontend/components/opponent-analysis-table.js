/**
 * OpponentAnalysisTable Component
 * A reusable component for displaying opponent analysis data with pagination
 */
class OpponentAnalysisTable {
    constructor(containerId, options = {}) {
        this.containerId = containerId;
        this.container = document.getElementById(containerId);
        
        // Default options
        this.options = {
            title: 'Opponent Analysis History',
            pageSize: 25,
            showControls: true,
            showManualControls: true, // Show load/refresh buttons by default
            apiBaseUrl: window.API_BASE_URL,
            apiEndpoint: '/api/user/analysis/history',
            onDataLoad: null,
            onError: null,
            autoRefresh: false,
            refreshInterval: 300000, // 5 minutes
            ...options
        };
        
        // Pagination state
        this.currentPage = 1;
        this.pageSize = this.options.pageSize;
        this.totalItems = 0;
        this.allData = [];
        
        this.refreshIntervalId = null;
        this.init();
    }
    
    init() {
        this.createHTML();
        this.bindEvents();
        
        // Start auto-refresh if enabled
        if (this.options.autoRefresh && this.options.refreshInterval > 0) {
            this.startAutoRefresh();
        }
    }
    
    createHTML() {
        const html = `
            <div class="opponent-analysis-table-container">
                <div class="opponent-analysis-header">
                    <h2 class="opponent-analysis-title">${this.options.title}</h2>
                </div>
                
                <div id="${this.containerId}-loading" class="opponent-analysis-loading hidden">
                    Loading analysis history...
                </div>
                
                <div id="${this.containerId}-error" class="opponent-analysis-error hidden">
                    <p id="${this.containerId}-error-message"></p>
                </div>
                
                <div id="${this.containerId}-empty" class="opponent-analysis-empty hidden">
                    <p>No opponent analysis history found.</p>
                    <p>Start listening for games to analyze your opponents!</p>
                </div>
                
                <div class="table-wrapper">
                    <table id="${this.containerId}-table" class="opponent-analysis-table hidden">
                        <thead>
                            <tr>
                                <th>Date</th>
                                <th>Opponent</th>
                                <th>Color</th>
                                <th class="analysis-result-header">Analysis Result</th>
                            </tr>
                        </thead>
                        <tbody id="${this.containerId}-tbody">
                            <!-- Table rows will be populated by JavaScript -->
                        </tbody>
                    </table>
                </div>
                
                ${this.options.showControls ? `
                <div id="${this.containerId}-pagination" class="pagination-controls hidden">
                    <div class="pagination-info">
                        <span id="${this.containerId}-pagination-info">Showing 1-10 of 50 entries</span>
                    </div>
                    <div class="pagination-buttons">
                        <button id="${this.containerId}-prev-btn" class="prev-btn" disabled>Previous</button>
                        <span id="${this.containerId}-page-numbers" class="page-numbers"></span>
                        <button id="${this.containerId}-next-btn" class="next-btn">Next</button>
                    </div>
                    <div class="pagination-size">
                        <label for="${this.containerId}-page-size-select">Show:</label>
                        <select id="${this.containerId}-page-size-select" class="page-size-select">
                            <option value="5" selected>5</option>    
                            <option value="10">10</option>
                            <option value="25" >25</option>
                            <option value="50">50</option>
                            <option value="100">100</option>
                        </select>
                        <span>per page</span>
                    </div>
                </div>
                ` : ''}
            </div>
        `;
        
        this.container.innerHTML = html;
    }
    
    bindEvents() {
        
        if (this.options.showControls) {
            // Pagination buttons
            document.getElementById(`${this.containerId}-prev-btn`).addEventListener('click', () => {
                this.changePage(-1);
            });
            
            document.getElementById(`${this.containerId}-next-btn`).addEventListener('click', () => {
                this.changePage(1);
            });
            
            // Page size selector
            document.getElementById(`${this.containerId}-page-size-select`).addEventListener('change', () => {
                this.changePageSize();
            });
        }
    }
    
    async loadData() {
        try {
            this.showLoading();
            this.hideError();
            
            console.log('Loading analysis history...');
            
            const response = await fetch(`${this.options.apiBaseUrl}${this.options.apiEndpoint}`, {
                method: 'GET',
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/json'
                }
            });

            if (response.status === 401) {
                this.showError('Authentication required. Please log in again.');
                return;
            }

            const data = await response.json();
            
            if (data.success) {
                console.log('Analysis history data:', data.analysis_history);
                this.displayData(data.analysis_history);
                
                if (this.options.onDataLoad) {
                    this.options.onDataLoad(data.analysis_history);
                }
            } else {
                this.showError(data.error || 'Failed to load analysis history');
            }

        } catch (error) {
            console.error('Error loading analysis history:', error);
            this.showError('Network error. Please check your connection.');
        }
    }
    
    async refreshData() {
        try {
            this.showLoading();
            this.hideError();
                        
            console.log('Refreshing analysis history...');
            
            // Reset pagination
            this.currentPage = 1;
            
            // Reload data
            await this.loadData();
            
        } catch (error) {
            console.error('Error refreshing analysis history:', error);
            this.showError('Failed to refresh analysis history: ' + error.message);
        }
    }
    
    displayData(historyData) {
        this.hideLoading();
        this.hideError();
        
        if (!historyData || historyData.length === 0) {
            // Show empty state
            document.getElementById(`${this.containerId}-empty`).classList.remove('hidden');
            document.getElementById(`${this.containerId}-table`).classList.add('hidden');
            if (this.options.showControls) {
                document.getElementById(`${this.containerId}-pagination`).classList.add('hidden');
            }
            return;
        }
        
        // Store all data for pagination
        this.allData = historyData;
        this.totalItems = historyData.length;
        
        // Hide empty state and show table
        document.getElementById(`${this.containerId}-empty`).classList.add('hidden');
        document.getElementById(`${this.containerId}-table`).classList.remove('hidden');
        
        if (this.options.showControls) {
            document.getElementById(`${this.containerId}-pagination`).classList.remove('hidden');
            this.updatePagination();
        }
        
        this.displayCurrentPage();
        
    }
    
    displayCurrentPage() {
        const tbody = document.getElementById(`${this.containerId}-tbody`);
        tbody.innerHTML = '';
        
        if (!this.options.showControls) {
            // If no pagination, show all data
            this.allData.forEach(analysis => {
                tbody.appendChild(this.createTableRow(analysis));
            });
            return;
        }
        
        const startIndex = (this.currentPage - 1) * this.pageSize;
        const endIndex = Math.min(startIndex + this.pageSize, this.totalItems);
        const pageData = this.allData.slice(startIndex, endIndex);
        
        pageData.forEach(analysis => {
            tbody.appendChild(this.createTableRow(analysis));
        });
    }
    
    createTableRow(analysis) {
        const row = document.createElement('tr');
        
        // Format date
        const date = new Date(analysis.created_at);
        const formattedDate = date.toLocaleDateString(); //+ ' ' + date.toLocaleTimeString();
        
        // Create color display
        const colorDisplay = analysis.color === 'white' ? 'White ♔' : 'Black ♚';
        const colorClass = analysis.color === 'white' ? 'white' : 'black';
        
        row.innerHTML = `
            <td class="date-cell">${formattedDate}</td>
            <td class="opponent-cell">${analysis.opponent}</td>
            <td class="color-cell ${colorClass}">${colorDisplay}</td>
            <td class="prep-cell">${analysis.preparation}</td>
        `;
        
        return row;
    }
    
    updatePagination() {
        if (!this.options.showControls) return;
        
        const totalPages = Math.ceil(this.totalItems / this.pageSize);
        const startIndex = (this.currentPage - 1) * this.pageSize + 1;
        const endIndex = Math.min(this.currentPage * this.pageSize, this.totalItems);
        
        // Update pagination info
        document.getElementById(`${this.containerId}-pagination-info`).textContent = 
            `Showing ${startIndex}-${endIndex} of ${this.totalItems} entries`;
        
        // Update previous/next buttons
        document.getElementById(`${this.containerId}-prev-btn`).disabled = this.currentPage === 1;
        document.getElementById(`${this.containerId}-next-btn`).disabled = this.currentPage === totalPages;
        
        // Update page numbers
        const pageNumbersContainer = document.getElementById(`${this.containerId}-page-numbers`);
        pageNumbersContainer.innerHTML = '';
        
        const maxVisiblePages = 5;
        let startPage = Math.max(1, this.currentPage - Math.floor(maxVisiblePages / 2));
        let endPage = Math.min(totalPages, startPage + maxVisiblePages - 1);
        
        if (endPage - startPage + 1 < maxVisiblePages) {
            startPage = Math.max(1, endPage - maxVisiblePages + 1);
        }
        
        for (let i = startPage; i <= endPage; i++) {
            const pageNumber = document.createElement('span');
            pageNumber.className = `page-number ${i === this.currentPage ? 'active' : ''}`;
            pageNumber.textContent = i;
            pageNumber.onclick = () => this.goToPage(i);
            pageNumbersContainer.appendChild(pageNumber);
        }
    }
    
    changePage(direction) {
        if (!this.options.showControls) return;
        
        const totalPages = Math.ceil(this.totalItems / this.pageSize);
        const newPage = this.currentPage + direction;
        
        if (newPage >= 1 && newPage <= totalPages) {
            this.currentPage = newPage;
            this.displayCurrentPage();
            this.updatePagination();
        }
    }
    
    goToPage(page) {
        if (!this.options.showControls) return;
        
        const totalPages = Math.ceil(this.totalItems / this.pageSize);
        if (page >= 1 && page <= totalPages) {
            this.currentPage = page;
            this.displayCurrentPage();
            this.updatePagination();
        }
    }
    
    changePageSize() {
        if (!this.options.showControls) return;
        
        this.pageSize = parseInt(document.getElementById(`${this.containerId}-page-size-select`).value);
        this.currentPage = 1; // Reset to first page
        this.displayCurrentPage();
        this.updatePagination();
    }
    
    showLoading() {
        document.getElementById(`${this.containerId}-loading`).classList.remove('hidden');
        document.getElementById(`${this.containerId}-table`).classList.add('hidden');
        document.getElementById(`${this.containerId}-error`).classList.add('hidden');
        document.getElementById(`${this.containerId}-empty`).classList.add('hidden');
        if (this.options.showControls) {
            document.getElementById(`${this.containerId}-pagination`).classList.add('hidden');
        }
    }
    
    hideLoading() {
        document.getElementById(`${this.containerId}-loading`).classList.add('hidden');
    }
    
    showError(message) {
        this.hideLoading();
        document.getElementById(`${this.containerId}-error`).classList.remove('hidden');
        document.getElementById(`${this.containerId}-table`).classList.add('hidden');
        if (this.options.showControls) {
            document.getElementById(`${this.containerId}-pagination`).classList.add('hidden');
        }
        document.getElementById(`${this.containerId}-error-message`).textContent = message;
        
        
        if (this.options.onError) {
            this.options.onError(message);
        }
    }
    
    hideError() {
        document.getElementById(`${this.containerId}-error`).classList.add('hidden');
    }
    
    // Public methods for external control
    setData(data) {
        this.displayData(data);
    }
    
    getData() {
        return this.allData;
    }
    
    // Auto-refresh methods
    startAutoRefresh() {
        if (this.refreshIntervalId) {
            clearInterval(this.refreshIntervalId);
        }
        
        this.refreshIntervalId = setInterval(() => {
            console.log('Auto-refreshing opponent analysis data...');
            this.refreshData();
        }, this.options.refreshInterval);
    }
    
    stopAutoRefresh() {
        if (this.refreshIntervalId) {
            clearInterval(this.refreshIntervalId);
            this.refreshIntervalId = null;
        }
    }
    
    destroy() {
        // Stop auto-refresh
        this.stopAutoRefresh();
        
        // Clean up event listeners and DOM
        this.container.innerHTML = '';
    }
}
