// Screenshot data with captions
const screenshotData = {
    'advanced-config.png': 'Advanced Config',
    'commands.png': 'Commands',
    'connection-settings.png': 'Connection Settings',
    'custom-terminal.png': 'Custom Terminal',
    'htop.png': 'Htop',
    'main-window.png': 'Main Window',
    'preferences.png': 'Preferences',
    'ssh-settings.png': 'Ssh Settings',
    'terminal-settings.png': 'Terminal Settings'
};

// Screenshot management class
class ScreenshotManager {
    constructor() {
        this.currentSlide = 0;
        this.isPlaying = false;
        this.autoPlayInterval = null;
        this.screenshots = [];
        
        this.init();
    }
    
    init() {
        this.loadScreenshots();
        this.setupEventListeners();
        this.renderGrid();
        this.renderSlideshow();
    }
    
    loadScreenshots() {
        // Load screenshots from the data object
        this.screenshots = Object.entries(screenshotData).map(([filename, caption]) => ({
            src: `screenshots/${filename}`,
            alt: caption,
            caption: caption
        }));
    }
    
    renderGrid() {
        const gridContainer = document.getElementById('grid-view');
        gridContainer.innerHTML = '';
        
        this.screenshots.forEach((screenshot, index) => {
            const item = document.createElement('div');
            item.className = 'screenshot-item';
            item.innerHTML = `
                <img src="${screenshot.src}" alt="${screenshot.alt}" class="screenshot-img" loading="lazy">
                <p class="screenshot-caption">${screenshot.caption}</p>
            `;
            
            // Add click handler to open in slideshow
            item.addEventListener('click', () => {
                this.switchToSlideshow();
                this.goToSlide(index);
            });
            
            gridContainer.appendChild(item);
        });
    }
    
    renderSlideshow() {
        this.updateSlideshowImage();
        this.renderIndicators();
        this.updateCounter();
    }
    
    updateSlideshowImage() {
        const slide = document.querySelector('.slideshow-slide');
        const img = slide.querySelector('.slideshow-img');
        const caption = slide.querySelector('.slideshow-caption');
        
        if (this.screenshots[this.currentSlide]) {
            img.src = this.screenshots[this.currentSlide].src;
            img.alt = this.screenshots[this.currentSlide].alt;
            caption.textContent = this.screenshots[this.currentSlide].caption;
        }
    }
    
    renderIndicators() {
        const indicatorsContainer = document.querySelector('.slideshow-indicators');
        indicatorsContainer.innerHTML = '';
        
        this.screenshots.forEach((_, index) => {
            const indicator = document.createElement('div');
            indicator.className = `slideshow-indicator ${index === this.currentSlide ? 'active' : ''}`;
            indicator.addEventListener('click', () => this.goToSlide(index));
            indicatorsContainer.appendChild(indicator);
        });
    }
    
    updateCounter() {
        const counter = document.querySelector('.slideshow-counter');
        counter.textContent = `${this.currentSlide + 1} / ${this.screenshots.length}`;
    }
    
    goToSlide(index) {
        this.currentSlide = index;
        this.updateSlideshowImage();
        this.renderIndicators();
        this.updateCounter();
    }
    
    nextSlide() {
        this.currentSlide = (this.currentSlide + 1) % this.screenshots.length;
        this.goToSlide(this.currentSlide);
    }
    
    prevSlide() {
        this.currentSlide = this.currentSlide === 0 ? this.screenshots.length - 1 : this.currentSlide - 1;
        this.goToSlide(this.currentSlide);
    }
    
    toggleAutoPlay() {
        if (this.isPlaying) {
            this.stopAutoPlay();
        } else {
            this.startAutoPlay();
        }
    }
    
    startAutoPlay() {
        this.isPlaying = true;
        const playPauseBtn = document.querySelector('.slideshow-play-pause');
        playPauseBtn.textContent = '⏸';
        
        this.autoPlayInterval = setInterval(() => {
            this.nextSlide();
        }, 3000);
    }
    
    stopAutoPlay() {
        this.isPlaying = false;
        const playPauseBtn = document.querySelector('.slideshow-play-pause');
        playPauseBtn.textContent = '▶';
        
        if (this.autoPlayInterval) {
            clearInterval(this.autoPlayInterval);
            this.autoPlayInterval = null;
        }
    }
    
    switchToSlideshow() {
        document.getElementById('grid-view').style.display = 'none';
        document.getElementById('slideshow-view').style.display = 'flex';
        
        // Update button states
        document.querySelector('[data-view="grid"]').classList.remove('active');
        document.querySelector('[data-view="slideshow"]').classList.add('active');
        
        // Prevent body scroll when slideshow is open
        document.body.style.overflow = 'hidden';
    }
    
    switchToGrid() {
        document.getElementById('grid-view').style.display = 'grid';
        document.getElementById('slideshow-view').style.display = 'none';
        
        // Update button states
        document.querySelector('[data-view="slideshow"]').classList.remove('active');
        document.querySelector('[data-view="grid"]').classList.add('active');
        
        // Restore body scroll
        document.body.style.overflow = 'auto';
    }
    
    setupEventListeners() {
        // View toggle buttons
        document.querySelectorAll('.view-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const view = btn.dataset.view;
                if (view === 'grid') {
                    this.switchToGrid();
                } else if (view === 'slideshow') {
                    this.switchToSlideshow();
                }
            });
        });
        
        // Slideshow navigation
        document.querySelector('.slideshow-prev').addEventListener('click', () => this.prevSlide());
        document.querySelector('.slideshow-next').addEventListener('click', () => this.nextSlide());
        document.querySelector('.slideshow-play-pause').addEventListener('click', () => this.toggleAutoPlay());
        document.querySelector('.slideshow-close').addEventListener('click', () => this.switchToGrid());
        
        // Keyboard navigation
        document.addEventListener('keydown', (e) => {
            if (document.getElementById('slideshow-view').style.display !== 'none') {
                if (e.key === 'ArrowLeft') {
                    this.prevSlide();
                } else if (e.key === 'ArrowRight') {
                    this.nextSlide();
                } else if (e.key === ' ') {
                    e.preventDefault();
                    this.toggleAutoPlay();
                } else if (e.key === 'Escape') {
                    this.switchToGrid();
                }
            }
        });
    }
}

// Initialize when DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
    new ScreenshotManager();
});
