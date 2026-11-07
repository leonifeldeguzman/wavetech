(function () {
  const valueDesc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');

  function init(input) {
    if (input.dataset.otpReady) return;
    input.dataset.otpReady = '1';
    const length = parseInt(input.dataset.otpBoxes, 10) || 6;

    const wrap = document.createElement('div');
    wrap.className = 'otp-boxes';
    wrap.setAttribute('role', 'group');
    const boxes = [];
    for (let i = 0; i < length; i++) {
      const b = document.createElement('input');
      b.type = 'text';
      b.className = 'otp-box';
      b.inputMode = 'numeric';
      b.maxLength = 1;
      b.autocomplete = i === 0 ? 'one-time-code' : 'off';
      b.setAttribute('aria-label', 'Digit ' + (i + 1) + ' of ' + length);
      wrap.appendChild(b);
      boxes.push(b);
    }
    input.type = 'hidden';
    input.insertAdjacentElement('afterend', wrap);

    function render(v) {
      const digits = (v || '').replace(/\D/g, '').slice(0, length);
      boxes.forEach((b, i) => { b.value = digits[i] || ''; });
    }
    function push() {
      valueDesc.set.call(input, boxes.map((b) => b.value).join(''));
      input.dispatchEvent(new Event('input', { bubbles: true }));
    }

    // Keep old code working: input.value = '' clears the boxes, input.focus() focuses them
    Object.defineProperty(input, 'value', {
      get() { return valueDesc.get.call(this); },
      set(v) { valueDesc.set.call(this, v); render(v); },
      configurable: true,
    });
    input.focus = function () {
      (boxes.find((b) => !b.value) || boxes[length - 1]).focus();
    };

    boxes.forEach((b, i) => {
      b.addEventListener('input', () => {
        b.value = b.value.replace(/\D/g, '').slice(-1);
        if (b.value && i < length - 1) boxes[i + 1].focus();
        push();
      });
      b.addEventListener('keydown', (e) => {
        if (e.key === 'Backspace' && !b.value && i > 0) {
          boxes[i - 1].value = '';
          boxes[i - 1].focus();
          push();
          e.preventDefault();
        } else if (e.key === 'ArrowLeft' && i > 0) {
          boxes[i - 1].focus();
          e.preventDefault();
        } else if (e.key === 'ArrowRight' && i < length - 1) {
          boxes[i + 1].focus();
          e.preventDefault();
        } else if (e.key === 'Enter') {
          input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
        }
      });
      b.addEventListener('focus', () => b.select());
      b.addEventListener('paste', (e) => {
        e.preventDefault();
        const text = (e.clipboardData || window.clipboardData)
          .getData('text').replace(/\D/g, '').slice(0, length);
        if (!text) return;
        render(text);
        push();
        boxes[Math.min(text.length, length - 1)].focus();
      });
    });

    render(valueDesc.get.call(input));
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('input[data-otp-boxes]').forEach(init);
  });
})();