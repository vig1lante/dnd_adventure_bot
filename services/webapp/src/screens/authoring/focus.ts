// Фокус вокруг вопросов конструктора: откуда вопрос задан и как вернуть туда фокус после ответа.
// Отдельно от компонентов: модуль с компонентами экспортирует только их (горячая перезагрузка).

/** Элемент в фокусе; `null` — фокуса ни у кого нет (он на странице в целом). */
export function focusedElement(): HTMLElement | null {
  const active = document.activeElement;
  return active instanceof HTMLElement && active !== document.body
    ? active
    : null;
}

// Сколько ждать, пока занятый запросом источник вопроса снова станет доступен; дольше — фокус не
// возвращается: автор за это время уже ушёл в другое место страницы.
const RETURN_FOCUS_WAIT_MS = 30_000;

/**
 * Возвращает фокус на `target` после ухода вопроса `dialog`, если фокус потерян (он на странице в
 * целом или остался в ушедшем вопросе), а `target` ещё на странице. Выключенный на время запроса
 * элемент браузер не фокусирует: фокус вернётся, как только его включат, если к тому времени фокус
 * никто не занял (например, следующий вопрос).
 */
export function returnFocusTo(target: HTMLElement, dialog: HTMLElement) {
  const settled = (): boolean => {
    const now = document.activeElement;
    const lost = now === null || now === document.body || dialog.contains(now);
    if (!target.isConnected || !lost) return true;
    if (target.matches(":disabled")) return false;
    target.focus();
    return true;
  };
  if (settled()) return;
  // Включение — снятие атрибута `disabled` у самой кнопки или у её `fieldset`.
  const observer = new MutationObserver(() => {
    if (settled()) observer.disconnect();
  });
  observer.observe(document.body, {
    subtree: true,
    attributes: true,
    attributeFilter: ["disabled"],
  });
  setTimeout(() => observer.disconnect(), RETURN_FOCUS_WAIT_MS);
}
