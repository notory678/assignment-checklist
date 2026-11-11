document.querySelectorAll('form[data-loading]').forEach((form) => {
  form.addEventListener('submit', () => {
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    button.textContent = form.dataset.loading;
    form.setAttribute('aria-busy', 'true');
  });
});
