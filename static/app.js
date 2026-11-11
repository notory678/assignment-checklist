document.querySelectorAll('form[data-loading]').forEach((form) => {
  form.addEventListener('submit', () => {
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    button.textContent = form.dataset.loading;
    form.setAttribute('aria-busy', 'true');
    const status = document.createElement('p');
    status.className = 'hint';
    status.setAttribute('role', 'status');
    status.textContent = form.dataset.loadingMessage || '결과를 준비하고 있어요. 로컬 AI는 분석에 시간이 더 걸릴 수 있습니다.';
    form.append(status);
  });
});

document.querySelectorAll('form[data-pdf-selection]').forEach((form) => {
  const pages = [...form.querySelectorAll('input[name="selected"]:not(:disabled)')];
  const summary = form.querySelector('[data-selection-summary]');
  const update = () => {
    const selected = pages.filter((page) => page.checked);
    const length = selected.reduce((total, page) => total + Number(page.dataset.textLength), 0)
      + Math.max(0, selected.length - 1) * 2;
    summary.textContent = `${selected.length}페이지 선택 · 본문 ${length.toLocaleString('ko-KR')}자 / 최대 30,000자`;
    summary.classList.toggle('over-limit', length > 30000);
  };
  pages.forEach((page) => page.addEventListener('change', update));
  form.querySelectorAll('[data-select-pages]').forEach((button) => {
    button.addEventListener('click', () => {
      pages.forEach((page) => { page.checked = button.dataset.selectPages === 'all'; });
      update();
    });
  });
  update();
});
