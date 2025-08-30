// Smooth scrolling for navigation links
document.addEventListener('DOMContentLoaded', function() {
    // Smooth scrolling for anchor links
    const links = document.querySelectorAll('a[href^="#"]');
    
    links.forEach(link => {
        link.addEventListener('click', function(e) {
            e.preventDefault();
            
            const targetId = this.getAttribute('href');
            const targetElement = document.querySelector(targetId);
            
            if (targetElement) {
                const headerHeight = document.querySelector('.header').offsetHeight;
                const targetPosition = targetElement.offsetTop - headerHeight - 20;
                
                window.scrollTo({
                    top: targetPosition,
                    behavior: 'smooth'
                });
            }
        });
    });

    // Add scroll effect to header
    const header = document.querySelector('.header');
    let lastScrollTop = 0;
    
    window.addEventListener('scroll', function() {
        const scrollTop = window.pageYOffset || document.documentElement.scrollTop;
        
        if (scrollTop > lastScrollTop && scrollTop > 100) {
            // Scrolling down
            header.style.transform = 'translateY(-100%)';
        } else {
            // Scrolling up
            header.style.transform = 'translateY(0)';
        }
        
        lastScrollTop = scrollTop;
    });

    // Add intersection observer for animations
    const observerOptions = {
        threshold: 0.1,
        rootMargin: '0px 0px -50px 0px'
    };

    const observer = new IntersectionObserver(function(entries) {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                entry.target.style.opacity = '1';
                entry.target.style.transform = 'translateY(0)';
            }
        });
    }, observerOptions);

    // Observe elements for animation
    const animateElements = document.querySelectorAll('.feature-card, .screenshot-item, .download-card, .shortcut-item');
    animateElements.forEach(el => {
        el.style.opacity = '0';
        el.style.transform = 'translateY(20px)';
        el.style.transition = 'opacity 0.6s ease-out, transform 0.6s ease-out';
        observer.observe(el);
    });

    // Add loading animation for images
    const images = document.querySelectorAll('img');
    images.forEach(img => {
        img.addEventListener('load', function() {
            this.style.opacity = '1';
        });
        
        // If image is already loaded
        if (img.complete) {
            img.style.opacity = '1';
        }
    });

    // Add keyboard navigation support
    document.addEventListener('keydown', function(e) {
        // Escape key to close any open modals or return to top
        if (e.key === 'Escape') {
            window.scrollTo({
                top: 0,
                behavior: 'smooth'
            });
        }
    });

    // Add copy functionality for cryptocurrency addresses
    const cryptoAddresses = document.querySelectorAll('.footer-section p');
    cryptoAddresses.forEach(address => {
        if (address.textContent.includes('bc1') || address.textContent.includes('DRz')) {
            address.style.cursor = 'pointer';
            address.title = 'Click to copy';
            
            address.addEventListener('click', function() {
                const text = this.textContent.split(': ')[1];
                navigator.clipboard.writeText(text).then(() => {
                    // Show temporary feedback
                    const originalText = this.textContent;
                    this.textContent = 'Copied!';
                    this.style.color = 'var(--primary-color)';
                    
                    setTimeout(() => {
                        this.textContent = originalText;
                        this.style.color = '';
                    }, 2000);
                });
            });
        }
    });

    // Add theme toggle functionality (if needed in the future)
    // This can be expanded to add a theme toggle button
    function updateTheme() {
        const isDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
        document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light');
    }

    // Listen for theme changes
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', updateTheme);
    updateTheme();

    // Add download tracking (optional analytics)
    const downloadButtons = document.querySelectorAll('a[href*="github.com/mfat/sshpilot/releases"]');
    downloadButtons.forEach(button => {
        button.addEventListener('click', function() {
            // You can add analytics tracking here
            console.log('Download clicked:', this.href);
        });
    });

    // Add mobile menu functionality (if needed)
    // This can be expanded to add a hamburger menu for mobile
    function setupMobileMenu() {
        const navLinks = document.querySelector('.nav-links');
        const header = document.querySelector('.header');
        
        // Add hamburger menu for mobile if needed
        if (window.innerWidth <= 768 && !document.querySelector('.mobile-menu-toggle')) {
            const toggle = document.createElement('button');
            toggle.className = 'mobile-menu-toggle';
            toggle.innerHTML = '☰';
            toggle.setAttribute('aria-label', 'Toggle navigation menu');
            
            toggle.addEventListener('click', function() {
                navLinks.classList.toggle('show');
                this.innerHTML = navLinks.classList.contains('show') ? '✕' : '☰';
            });
            
            header.querySelector('.nav-container').appendChild(toggle);
        }
    }

    // Setup mobile menu on load and resize
    setupMobileMenu();
    window.addEventListener('resize', setupMobileMenu);
});

// Add service worker for PWA capabilities (optional)
if ('serviceWorker' in navigator) {
    window.addEventListener('load', function() {
        // You can register a service worker here for offline functionality
        // navigator.serviceWorker.register('/sw.js');
    });
}
