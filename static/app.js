document.querySelectorAll('form[data-loading]').forEach((form) => {
  form.addEventListener('submit', () => {
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    button.textContent = form.dataset.loading;
    form.setAttribute('aria-busy', 'true');
    const status = document.createElement('p');
    status.className = 'hint';
    status.setAttribute('role', 'status');
    status.textContent = '결과를 준비하고 있어요. 로컬 AI는 분석에 시간이 더 걸릴 수 있습니다.';
    form.append(status);
  });
});
