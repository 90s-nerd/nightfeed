(() => {
  document.querySelectorAll('[data-security-entries]').forEach(group => {
    const rows = () => [...group.querySelectorAll('[data-entry-row]')];
    const update = () => rows().forEach(row => {
      row.querySelector('[data-entry-add]').disabled = rows().length >= 20;
      row.querySelector('[data-entry-remove]').disabled = rows().length === 1 &&
        [...row.querySelectorAll('input:not([type=number])')].every(input => !input.value.trim());
    });
    group.addEventListener('click', event => {
      const button = event.target.closest('[data-entry-add], [data-entry-remove]');
      if (!button || button.disabled) return;
      const row = button.closest('[data-entry-row]');
      if (button.hasAttribute('data-entry-add')) {
        const next = row.cloneNode(true);
        next.querySelectorAll('input').forEach(input => { input.value = input.type === 'number' ? '1' : ''; });
        row.after(next);
        next.querySelector('input').focus();
      } else if (rows().length === 1) {
        row.querySelectorAll('input').forEach(input => { input.value = input.type === 'number' ? '1' : ''; });
        row.querySelector('input').focus();
      } else {
        const target = row.nextElementSibling || row.previousElementSibling;
        row.remove();
        target.querySelector('input').focus();
      }
      update();
      group.dispatchEvent(new Event('input', { bubbles: true }));
    });
    group.addEventListener('input', update);
    update();
  });
})();
