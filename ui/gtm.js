/**
 * Google Tag Manager initialization
 * This script initializes GTM on all pages without modifying HTML directly
 */

// GTM Container ID
const GTM_ID = 'GTM-KRBGTMQ4';

// Initialize dataLayer if not already present
window.dataLayer = window.dataLayer || [];

// GTM initialization function
function initGTM() {
  // Create and inject GTM script
  const script = document.createElement('script');
  script.async = true;
  script.src = `https://www.googletagmanager.com/gtag/js?id=${GTM_ID}`;
  document.head.appendChild(script);

  // gtag function definition
  window.gtag = function() {
    window.dataLayer.push(arguments);
  };
  window.gtag('js', new Date());
  window.gtag('config', GTM_ID);

  // Inject noscript iframe for GTM fallback
  const noscript = document.createElement('noscript');
  const iframe = document.createElement('iframe');
  iframe.src = `https://www.googletagmanager.com/ns.html?id=${GTM_ID}`;
  iframe.height = '0';
  iframe.width = '0';
  iframe.style.display = 'none';
  iframe.style.visibility = 'hidden';
  noscript.appendChild(iframe);
  document.body.insertBefore(noscript, document.body.firstChild);
}

// Initialize GTM when DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initGTM);
} else {
  initGTM();
}

/**
 * Helper functions to track events via GTM
 */

// Track page views (automatically handled by GTM config above)
window.trackPageView = function(pageName, additionalData = {}) {
  window.gtag('event', 'page_view', {
    page_title: pageName,
    ...additionalData
  });
};

// Track custom events (e.g., form submissions, clicks, purchases)
window.trackEvent = function(eventName, eventData = {}) {
  window.gtag('event', eventName, eventData);
};

// Track product view
window.trackProductView = function(productId, productName, productCategory = '', price = null) {
  window.gtag('event', 'view_item', {
    currency: 'ARS',
    value: price || 0,
    items: [{
      item_id: productId,
      item_name: productName,
      item_category: productCategory,
      price: price
    }]
  });
};

// Track add to cart
window.trackAddToCart = function(productId, productName, quantity = 1, price = null) {
  window.gtag('event', 'add_to_cart', {
    currency: 'ARS',
    value: (price || 0) * quantity,
    items: [{
      item_id: productId,
      item_name: productName,
      quantity: quantity,
      price: price
    }]
  });
};

// Track purchase
window.trackPurchase = function(orderId, totalValue, items = []) {
  window.gtag('event', 'purchase', {
    transaction_id: orderId,
    currency: 'ARS',
    value: totalValue,
    items: items
  });
};

// Track form submission
window.trackFormSubmit = function(formName, additionalData = {}) {
  window.gtag('event', 'form_submit', {
    form_name: formName,
    ...additionalData
  });
};

// Track game page view (called when viewing individual game)
window.trackGameView = function(gameName, gamePrice, gamePlatform = '') {
  window.dataLayer.push({
    event: 'view_item',
    game_name: gameName,
    game_price: gamePrice,
    game_platform: gamePlatform
  });
  window.gtag('event', 'view_item', {
    items: [{
      item_id: gameName,
      item_name: gameName,
      item_category: gamePlatform,
      price: gamePrice
    }]
  });
};

// Track checkout initiation (when clicking Buy button)
window.trackCheckoutStart = function(gameName, gamePrice) {
  window.dataLayer.push({
    event: 'begin_checkout',
    game_name: gameName,
    game_price: gamePrice
  });
  window.gtag('event', 'begin_checkout', {
    currency: 'ARS',
    value: gamePrice,
    items: [{
      item_name: gameName,
      price: gamePrice
    }]
  });
};

// Track lead (WhatsApp inquiry)
window.trackLead = function(gameName, leadType = 'whatsapp') {
  window.dataLayer.push({
    event: 'generate_lead',
    game_name: gameName,
    lead_type: leadType
  });
  window.gtag('event', 'generate_lead', {
    lead_type: leadType,
    game_name: gameName
  });
};

console.log('✅ Google Tag Manager initialized with ID:', GTM_ID);
