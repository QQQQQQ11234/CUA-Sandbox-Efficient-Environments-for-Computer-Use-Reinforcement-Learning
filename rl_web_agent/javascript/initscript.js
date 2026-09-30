(() => {
  const ORIG = EventTarget.prototype.addEventListener;
  const HOVER = new Set(['mouseenter', 'mouseover', 'pointerenter']);
  EventTarget.prototype.addEventListener = function (type, listener, opts) {
    if (HOVER.has(type)) {
      try {
        console.log("hover event", type);
        // only real elements (skip window / document)
        if (this && this.setAttribute) {
          console.log("setting attribute", this);
          this.setAttribute('data-maybe-hoverable', 'true');
        }
      } catch (_) { /* ignore edge‑cases */ }
    }
    return ORIG.call(this, type, listener, opts);
  };

  // Network activity tracking for custom idle detection
  window.__networkActivity = {
    activeRequests: 0,
    lastActivity: Date.now(),
    eventTarget: new EventTarget(),

    // Emit network activity events
    _emitEvent: function(type, data = {}) {
      this.eventTarget.dispatchEvent(new CustomEvent(type, { detail: data }));
    },

    // Track XHR requests
    trackXHR: function() {
      const originalXHR = window.XMLHttpRequest;
      window.XMLHttpRequest = function() {
        const xhr = new originalXHR();

        const originalSend = xhr.send;
        xhr.send = function() {
          let completed = false;
          const onComplete = () => {
            if (completed) return;
            completed = true;
            window.__networkActivity.activeRequests--;
            window.__networkActivity.lastActivity = Date.now();
            window.__networkActivity._emitEvent('request-complete', {
              type: 'xhr',
              active: window.__networkActivity.activeRequests
            });
            console.log('XHR completed, active:', window.__networkActivity.activeRequests);
          };

          // An XHR is active only after send(), not after open(). Some apps
          // prepare requests that they never send; counting open() leaked the
          // counter forever and made every observation wait for the full
          // network-idle timeout. loadend covers success, error and abort.
          window.__networkActivity.activeRequests++;
          window.__networkActivity.lastActivity = Date.now();
          window.__networkActivity._emitEvent('request-start', {
            type: 'xhr',
            active: window.__networkActivity.activeRequests
          });
          console.log('XHR started, active:', window.__networkActivity.activeRequests);
          xhr.addEventListener('loadend', onComplete, { once: true });

          try {
            return originalSend.apply(this, arguments);
          } catch (error) {
            onComplete();
            throw error;
          }
        };

        return xhr;
      };
    },

    // Track fetch requests
    trackFetch: function() {
      const originalFetch = window.fetch;
      window.fetch = function() {
        window.__networkActivity.activeRequests++;
        window.__networkActivity.lastActivity = Date.now();
        window.__networkActivity._emitEvent('request-start', {
          type: 'fetch',
          active: window.__networkActivity.activeRequests
        });
        console.log('Fetch started, active:', window.__networkActivity.activeRequests);

        return originalFetch.apply(this, arguments).finally(() => {
          window.__networkActivity.activeRequests--;
          window.__networkActivity.lastActivity = Date.now();
          window.__networkActivity._emitEvent('request-complete', {
            type: 'fetch',
            active: window.__networkActivity.activeRequests
          });
          console.log('Fetch completed, active:', window.__networkActivity.activeRequests);
        });
      };
    },

    // Check if network is idle (synchronous version)
    isIdle: function(idleTimeMs = 500) {
      const now = Date.now();
      return this.activeRequests === 0 &&
             (now - this.lastActivity) >= idleTimeMs;
    },

    // Wait for network idle (simplified without AbortController)
    waitForIdle: function(idleTimeMs = 500, timeoutMs = 10000) {
      console.log(`waitForIdle called: idleTime=${idleTimeMs}ms, timeout=${timeoutMs}ms`);
      console.log(`Current state: activeRequests=${this.activeRequests}, lastActivity=${Date.now() - this.lastActivity}ms ago`);

      return new Promise((resolve) => {
        let idleTimeoutId = null;
        let timeoutId = null;
        let resolved = false;

        // Cleanup function
        const cleanup = () => {
          if (resolved) return;
          resolved = true;

          if (timeoutId) clearTimeout(timeoutId);
          if (idleTimeoutId) clearTimeout(idleTimeoutId);
          this.eventTarget.removeEventListener('request-start', onRequestStart);
          this.eventTarget.removeEventListener('request-complete', onRequestComplete);
          console.log('Cleanup completed');
        };

        // Safe resolve function
        const safeResolve = (value) => {
          if (!resolved) {
            cleanup();
            resolve(value);
          }
        };

        // Check if idle and start waiting
        const checkIdle = () => {
          if (resolved) return;

          const now = Date.now();
          const timeSinceLastActivity = now - this.lastActivity;
          const hasActiveRequests = this.activeRequests > 0;

          console.log(`checkIdle: activeRequests=${this.activeRequests}, timeSinceLastActivity=${timeSinceLastActivity}ms, needIdle=${idleTimeMs}ms`);

          if (hasActiveRequests) {
            // Clear any pending idle timeout - we have active requests
            if (idleTimeoutId) {
              console.log('Clearing idle timeout due to active requests');
              clearTimeout(idleTimeoutId);
              idleTimeoutId = null;
            }
            console.log('Has active requests, waiting for completion');
            return;
          }

          // No active requests, check if we've been idle long enough
          if (timeSinceLastActivity >= idleTimeMs) {
            console.log('Already idle for required time, resolving immediately');
            safeResolve(true);
            return;
          }

          // Need to wait more time. Calculate remaining time needed.
          const remainingTime = idleTimeMs - timeSinceLastActivity;
          console.log(`Need to wait ${remainingTime}ms more for idle (${timeSinceLastActivity}ms elapsed of ${idleTimeMs}ms needed)`);

          // Clear any existing timeout
          if (idleTimeoutId) {
            clearTimeout(idleTimeoutId);
          }

          // Set timeout for remaining time
          idleTimeoutId = setTimeout(() => {
            if (!resolved) {
              console.log(`Timeout fired after ${remainingTime}ms, double-checking idle state`);
              // Double-check that we're still idle
              const finalTimeSinceLastActivity = Date.now() - this.lastActivity;
              if (this.activeRequests === 0 && finalTimeSinceLastActivity >= idleTimeMs) {
                console.log(`Network idle confirmed: ${finalTimeSinceLastActivity}ms >= ${idleTimeMs}ms, resolving true`);
                safeResolve(true);
              } else {
                console.log(`Idle check failed: activeRequests=${this.activeRequests}, timeSinceLastActivity=${finalTimeSinceLastActivity}ms`);
                // Restart the check
                checkIdle();
              }
            }
          }, remainingTime);
        };

        // Listen for network activity
        const onRequestStart = () => {
          if (resolved) return;
          console.log('Request started, clearing idle timeout');
          if (idleTimeoutId) {
            clearTimeout(idleTimeoutId);
            idleTimeoutId = null;
          }
        };

        const onRequestComplete = () => {
          if (resolved) return;
          console.log('Request completed, checking idle in 50ms');
          // Small delay to let any follow-up requests start
          setTimeout(checkIdle, 50);
        };

        this.eventTarget.addEventListener('request-start', onRequestStart);
        this.eventTarget.addEventListener('request-complete', onRequestComplete);

        // Overall timeout
        timeoutId = setTimeout(() => {
          console.log('Overall timeout reached, resolving false');
          safeResolve(false); // Timeout, not idle
        }, timeoutMs);

        // Initial check
        console.log('Performing initial idle check');
        checkIdle();
      });
    },

    // Simple status check
    getStatus: function() {
      return {
        activeRequests: this.activeRequests,
        lastActivity: this.lastActivity,
        timeSinceLastActivity: Date.now() - this.lastActivity
      };
    }
  };

  // Initialize tracking
  window.__networkActivity.trackXHR();
  window.__networkActivity.trackFetch();

  console.log("initscript.js loaded with network tracking");
})();
