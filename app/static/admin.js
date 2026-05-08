(function () {
  'use strict';

  function readCookie(name) {
    const prefix = name + '=';
    const found = document.cookie
      .split(';')
      .map(function (chunk) { return chunk.trim(); })
      .find(function (chunk) { return chunk.startsWith(prefix); });
    return found ? found.slice(prefix.length) : '';
  }

  function injectCsrfTokens(token) {
    if (!token) return;
    const forms = document.querySelectorAll('form[method="post"], form[method="POST"]');
    forms.forEach(function (form) {
      if (form.querySelector('input[name="csrf_token"]')) return;
      const input = document.createElement('input');
      input.type = 'hidden';
      input.name = 'csrf_token';
      input.value = token;
      form.appendChild(input);
    });
  }

  function enhanceTooltips() {
    document.querySelectorAll('[data-tooltip]').forEach(function (el) {
      if (!el.hasAttribute('tabindex')) el.setAttribute('tabindex', '0');
      if (!el.hasAttribute('aria-label')) el.setAttribute('aria-label', el.getAttribute('data-tooltip'));
    });
  }

  function bindConfirmHandlers() {
    document.addEventListener('submit', function (event) {
      const form = event.target;
      if (!(form instanceof HTMLFormElement)) return;
      const message = form.getAttribute('data-confirm');
      if (!message) return;
      if (!window.confirm(message)) {
        event.preventDefault();
      }
    }, true);
  }

  document.addEventListener('DOMContentLoaded', function () {
    const cookieName = (document.body && document.body.dataset.csrfCookieName) || 'web_admin_csrf';
    injectCsrfTokens(readCookie(cookieName));
    enhanceTooltips();
    bindConfirmHandlers();
  });
})();
