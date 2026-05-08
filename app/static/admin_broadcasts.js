(function () {
  'use strict';

  const runAtInput = document.getElementById('broadcast-run-at');

  function toDatetimeLocalValue(date) {
    const pad = function (n) { return String(n).padStart(2, '0'); };
    return [
      date.getFullYear(),
      '-',
      pad(date.getMonth() + 1),
      '-',
      pad(date.getDate()),
      'T',
      pad(date.getHours()),
      ':',
      pad(date.getMinutes()),
    ].join('');
  }

  function setMinutesFromNow(minutes) {
    if (!runAtInput) return;
    const now = new Date();
    now.setSeconds(0, 0);
    now.setMinutes(now.getMinutes() + minutes);
    runAtInput.value = toDatetimeLocalValue(now);
  }

  function setTomorrowAt(hour) {
    if (!runAtInput) return;
    const dt = new Date();
    dt.setDate(dt.getDate() + 1);
    dt.setHours(hour, 0, 0, 0);
    runAtInput.value = toDatetimeLocalValue(dt);
  }

  document.querySelectorAll('.broadcast-quick-fill').forEach(function (btn) {
    btn.addEventListener('click', function () {
      const minutes = parseInt(btn.dataset.offsetMinutes || '0', 10);
      if (!Number.isNaN(minutes) && minutes > 0) {
        setMinutesFromNow(minutes);
      }
    });
  });

  document.querySelectorAll('.broadcast-tomorrow-fill').forEach(function (btn) {
    btn.addEventListener('click', function () {
      const hour = parseInt(btn.dataset.tomorrowHour || '10', 10);
      if (!Number.isNaN(hour)) {
        setTomorrowAt(hour);
      }
    });
  });

  const clearBtn = document.getElementById('broadcast-clear-run-at');
  if (clearBtn) {
    clearBtn.addEventListener('click', function () {
      if (runAtInput) runAtInput.value = '';
    });
  }

  const keyboardField = document.querySelector('textarea[name="keyboard_json"]');
  const fillKeyboardBtn = document.getElementById('broadcast-fill-keyboard-sample');
  const clearKeyboardBtn = document.getElementById('broadcast-clear-keyboard');

  if (fillKeyboardBtn && keyboardField) {
    fillKeyboardBtn.addEventListener('click', function () {
      keyboardField.value = JSON.stringify([
        [{ text: 'Открыть сайт', url: 'https://example.com' }],
        [{ text: 'Поддержка', callback_data: 'support:open' }],
      ], null, 2);
    });
  }

  if (clearKeyboardBtn && keyboardField) {
    clearKeyboardBtn.addEventListener('click', function () {
      keyboardField.value = '';
    });
  }
})();
