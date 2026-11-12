document.querySelectorAll('form[data-loading]').forEach((form) => {
  form.addEventListener('submit', () => {
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    button.textContent = form.dataset.loading;
    form.setAttribute('aria-busy', 'true');
    const status = document.createElement('p');
    status.className = 'hint';
    status.setAttribute('role', 'status');
    status.textContent = form.dataset.loadingMessage || '결과를 준비하고 있어요. 잠시만 기다려 주세요.';
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

// Toggle completion without reloading: keep the scroll position, move completed items to the bottom.
document.querySelectorAll('[data-checklist]').forEach((list) => {
  const counter = document.getElementById('completion-count');
  const itemId = (item) => Number(item.id.replace('checklist-item-', ''));

  const place = (item, completed) => {
    const others = [...list.children].filter((other) => other !== item);
    // Same order as the server: open items by id, then completed items by id.
    const next = others.find((other) => {
      const otherCompleted = other.classList.contains('completed');
      if (otherCompleted !== completed) return otherCompleted && !completed;
      return itemId(other) > itemId(item);
    });
    list.insertBefore(item, next || null);
  };

  list.addEventListener('submit', async (event) => {
    const form = event.target.closest('[data-completion-form]');
    if (!form) return;
    event.preventDefault();
    const button = form.querySelector('.check-toggle');
    const item = form.closest('.checklist-item');
    const completed = button.value === 'true';
    const following = item.nextElementSibling;
    button.disabled = true;
    try {
      const response = await fetch(form.action, {
        method: 'POST',
        headers: { 'X-Requested-With': 'fetch' },
        body: new URLSearchParams({ completed: String(completed) }),
      });
      if (!response.ok) throw new Error(String(response.status));
      const result = await response.json();
      item.classList.toggle('completed', result.completed);
      button.value = result.completed ? 'false' : 'true';
      button.setAttribute('aria-pressed', String(result.completed));
      item.querySelector('.kind-badge.done').hidden = !result.completed;
      if (counter) counter.textContent = `${result.completed_count}/${result.total} 완료`;
      const top = item.getBoundingClientRect().top;
      place(item, result.completed);
      // Keep the next item under the pointer and give keyboard users a sensible focus.
      const focusTarget = (following && following !== item ? following : item).querySelector('.check-toggle');
      if (focusTarget) focusTarget.focus({ preventScroll: true });
      if (following && following !== item) {
        window.scrollBy(0, following.getBoundingClientRect().top - top);
      }
    } catch (error) {
      const field = document.createElement('input');
      field.type = 'hidden';
      field.name = 'completed';
      field.value = String(completed);
      form.append(field);
      form.submit();  // fall back to the normal form post
    } finally {
      button.disabled = false;
    }
  });
});
