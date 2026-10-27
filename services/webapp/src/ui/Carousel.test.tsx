import { fireEvent, render, screen, within } from "@testing-library/react";
import { readSource } from "../styles/read-source";
import { Carousel } from ".";

const uiCss = (await readSource("./ui.css")).replace(/\/\*[\s\S]*?\*\//g, "");

/** Тело CSS-правила для селектора ровно из одного класса. */
const ruleOf = (className: string) =>
  new RegExp(`\\.${className}\\s*\\{([^}]*)\\}`).exec(uiCss)?.[1] ?? "";

const RACES = [
  { id: "human", name: "Человек" },
  { id: "elf", name: "Эльф" },
  { id: "dwarf", name: "Дварф" },
  { id: "halfling", name: "Полурослик" },
];

function renderCarousel(initialId?: string | null, onPick = vi.fn()) {
  render(
    <Carousel label="Раса" items={RACES} initialId={initialId}>
      {(item) => (
        <div>
          <h3>{item.name}</h3>
          <button type="button" onClick={() => onPick(item.id)}>
            Выбрать
          </button>
        </div>
      )}
    </Carousel>,
  );
  return onPick;
}

const carousel = () => screen.getByRole("group", { name: "Раса" });
const shown = () => screen.getByRole("heading", { level: 3 });
const counter = () => screen.getByText(/^\d+ из \d+$/);
const arrow = (name: "Предыдущая" | "Следующая") =>
  screen.getByRole("button", { name });

/**
 * Основной указатель — левая кнопка мыши, как её шлёт браузер. В jsdom у PointerEvent по
 * умолчанию `isPrimary: false`, поэтому признаки основного указателя задаются явно.
 */
const PRIMARY = {
  pointerId: 1,
  isPrimary: true,
  pointerType: "mouse",
  button: 0,
} as const;

const press = (target: Element, init: PointerEventInit = {}) =>
  fireEvent.pointerDown(target, { ...PRIMARY, ...init });
const release = (target: Element, init: PointerEventInit = {}) =>
  fireEvent.pointerUp(target, { ...PRIMARY, ...init });

function swipe(from: number, to: number, dy = 0) {
  const area = carousel();
  press(area, { clientX: from, clientY: 100 });
  release(area, { clientX: to, clientY: 100 + dy });
}

describe("Carousel", () => {
  test("корень — именованная группа с ролью «карусель»", () => {
    renderCarousel();

    expect(carousel()).toHaveAttribute("aria-roledescription", "карусель");
  });

  test("одна карточка на экране и счётчик «1 из 4»; без выбора — первая", () => {
    renderCarousel();

    expect(screen.getAllByRole("heading", { level: 3 })).toHaveLength(1);
    expect(shown()).toHaveTextContent("Человек");
    expect(counter()).toHaveTextContent("1 из 4");
  });

  test("начальная карточка — выбранный вариант; неизвестный id — первая", () => {
    renderCarousel("dwarf");
    expect(shown()).toHaveTextContent("Дварф");
    expect(counter()).toHaveTextContent("3 из 4");
  });

  test("неизвестный начальный id открывает первую карточку", () => {
    renderCarousel("orc");
    expect(shown()).toHaveTextContent("Человек");
  });

  test("стрелки листают вперёд и назад, счётчик следует", () => {
    renderCarousel();

    fireEvent.click(arrow("Следующая"));
    expect(shown()).toHaveTextContent("Эльф");
    expect(counter()).toHaveTextContent("2 из 4");
    fireEvent.click(arrow("Следующая"));
    expect(shown()).toHaveTextContent("Дварф");
    fireEvent.click(arrow("Предыдущая"));
    expect(shown()).toHaveTextContent("Эльф");
  });

  test("без зацикливания: на первой недоступна «Предыдущая», на последней — «Следующая»", () => {
    renderCarousel();
    expect(arrow("Предыдущая")).toBeDisabled();
    expect(arrow("Следующая")).toBeEnabled();

    for (let i = 0; i < 5; i++) fireEvent.click(arrow("Следующая"));

    expect(shown()).toHaveTextContent("Полурослик");
    expect(counter()).toHaveTextContent("4 из 4");
    expect(arrow("Следующая")).toBeDisabled();
    expect(arrow("Предыдущая")).toBeEnabled();
  });

  test("точки — кнопка на каждый вариант с его названием; текущая отмечена", () => {
    renderCarousel("elf");

    const dots = ["Человек", "Эльф", "Дварф", "Полурослик"].map((name) =>
      within(carousel()).getByRole("button", { name }),
    );
    expect(dots.map((dot) => dot.getAttribute("aria-current"))).toEqual([
      null,
      "true",
      null,
      null,
    ]);

    fireEvent.click(dots[3]);

    expect(shown()).toHaveTextContent("Полурослик");
    expect(counter()).toHaveTextContent("4 из 4");
    expect(dots[3]).toHaveAttribute("aria-current", "true");
    expect(dots[1]).not.toHaveAttribute("aria-current");
  });

  test("клавиши ←/→ при фокусе внутри листают и не зацикливаются", () => {
    renderCarousel();
    const inside = screen.getByRole("button", { name: "Выбрать" });

    fireEvent.keyDown(inside, { key: "ArrowLeft" });
    expect(shown()).toHaveTextContent("Человек");
    fireEvent.keyDown(inside, { key: "ArrowRight" });
    expect(shown()).toHaveTextContent("Эльф");
    fireEvent.keyDown(arrow("Следующая"), { key: "ArrowRight" });
    fireEvent.keyDown(arrow("Следующая"), { key: "ArrowRight" });
    fireEvent.keyDown(arrow("Следующая"), { key: "ArrowRight" });
    expect(shown()).toHaveTextContent("Полурослик");
    fireEvent.keyDown(carousel(), { key: "ArrowLeft" });
    expect(shown()).toHaveTextContent("Дварф");
  });

  test("другие клавиши карточку не меняют", () => {
    renderCarousel();
    fireEvent.keyDown(carousel(), { key: "ArrowDown" });
    fireEvent.keyDown(carousel(), { key: "Enter" });
    expect(shown()).toHaveTextContent("Человек");
  });

  test("свайп влево — следующая, вправо — предыдущая; на краях без зацикливания", () => {
    renderCarousel();

    swipe(300, 100);
    expect(shown()).toHaveTextContent("Эльф");
    swipe(100, 300);
    expect(shown()).toHaveTextContent("Человек");
    swipe(100, 300);
    expect(shown()).toHaveTextContent("Человек");
  });

  test("короткий или вертикальный жест — не листание", () => {
    renderCarousel();

    swipe(200, 180);
    expect(shown()).toHaveTextContent("Человек");
    swipe(300, 200, 150);
    expect(shown()).toHaveTextContent("Человек");
  });

  test("отменённый жест (pointercancel) не листает", () => {
    renderCarousel();
    const area = carousel();

    press(area, { clientX: 300, clientY: 100 });
    fireEvent.pointerCancel(area, PRIMARY);
    release(area, { clientX: 100, clientY: 100 });

    expect(shown()).toHaveTextContent("Человек");
  });

  test("щелчок, которым закончился свайп мышью, кнопку в карточке не нажимает", () => {
    const onPick = renderCarousel();
    const pick = screen.getByRole("button", { name: "Выбрать" });

    press(pick, { clientX: 300, clientY: 100 });
    release(pick, { clientX: 100, clientY: 100 });
    fireEvent.click(screen.getByRole("button", { name: "Выбрать" }));

    expect(shown()).toHaveTextContent("Эльф");
    expect(onPick).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Выбрать" }));
    expect(onPick).toHaveBeenCalledWith("elf");
  });

  test("обычное нажатие без сдвига кнопку в карточке нажимает", () => {
    const onPick = renderCarousel();
    const pick = screen.getByRole("button", { name: "Выбрать" });

    press(pick, { clientX: 100, clientY: 100 });
    release(pick, { clientX: 102, clientY: 101 });
    fireEvent.click(pick);

    expect(onPick).toHaveBeenCalledWith("human");
  });

  test("стрелки и точки — обычные кнопки: форму вокруг не отправляют", () => {
    const onSubmit = vi.fn((event: Event) => event.preventDefault());
    render(
      <form onSubmit={(event) => onSubmit(event.nativeEvent)}>
        <Carousel label="Класс" items={RACES}>
          {(item) => <p>{item.name}</p>}
        </Carousel>
      </form>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Следующая" }));
    fireEvent.click(screen.getByRole("button", { name: "Дварф" }));

    expect(onSubmit).not.toHaveBeenCalled();
  });

  test("пустой список — ничего не показано", () => {
    const { container } = render(
      <Carousel label="Раса" items={[]}>
        {() => <p>карточка</p>}
      </Carousel>,
    );
    expect(container).toBeEmptyDOMElement();
  });
});

// Финальное покрытие блока 7: пограничные случаи листания, клавиш и свайпа.
describe("Carousel: пограничные случаи", () => {
  /** Карусель с заголовком карточки; без кнопки «Выбрать». */
  const plain = (items: readonly { id: string; name: string }[]) => (
    <Carousel label="Раса" items={items} initialId="halfling">
      {(item) => <h3>{item.name}</h3>}
    </Carousel>
  );

  test("один вариант: «1 из 1», обе стрелки недоступны; клавиши и свайп его не меняют", () => {
    render(plain([RACES[2]]));

    expect(counter()).toHaveTextContent("1 из 1");
    expect(arrow("Предыдущая")).toBeDisabled();
    expect(arrow("Следующая")).toBeDisabled();
    expect(
      within(carousel()).getByRole("button", { name: "Дварф" }),
    ).toHaveAttribute("aria-current", "true");

    fireEvent.keyDown(carousel(), { key: "ArrowRight" });
    fireEvent.keyDown(carousel(), { key: "ArrowLeft" });
    swipe(300, 100);
    swipe(100, 300);

    expect(shown()).toHaveTextContent("Дварф");
    expect(counter()).toHaveTextContent("1 из 1");
  });

  test("вариантов стало меньше на ходу: показана последняя карточка, стрелки и точки по новому списку", () => {
    const { rerender } = render(plain(RACES));
    expect(counter()).toHaveTextContent("4 из 4");

    rerender(plain(RACES.slice(0, 2)));

    expect(shown()).toHaveTextContent("Эльф");
    expect(counter()).toHaveTextContent("2 из 2");
    expect(arrow("Следующая")).toBeDisabled();
    expect(arrow("Предыдущая")).toBeEnabled();
    expect(
      within(carousel()).getByRole("button", { name: "Эльф" }),
    ).toHaveAttribute("aria-current", "true");
    expect(
      within(carousel()).queryByRole("button", { name: "Полурослик" }),
    ).toBeNull();
    // Листание идёт от показанной карточки, а не от прежней позиции за краем списка.
    fireEvent.click(arrow("Предыдущая"));
    expect(shown()).toHaveTextContent("Человек");
    expect(counter()).toHaveTextContent("1 из 2");
  });

  test("вариантов стало больше на ходу: показанная карточка остаётся, «Следующая» снова доступна", () => {
    const { rerender } = render(
      <Carousel label="Раса" items={RACES.slice(0, 2)} initialId="elf">
        {(item) => <h3>{item.name}</h3>}
      </Carousel>,
    );
    expect(arrow("Следующая")).toBeDisabled();

    rerender(
      <Carousel label="Раса" items={RACES} initialId="elf">
        {(item) => <h3>{item.name}</h3>}
      </Carousel>,
    );

    expect(shown()).toHaveTextContent("Эльф");
    expect(counter()).toHaveTextContent("2 из 4");
    fireEvent.click(arrow("Следующая"));
    expect(shown()).toHaveTextContent("Дварф");
  });

  test("список опустел и снова наполнился: карусель пропадает и возвращается без сбоя", () => {
    const { container, rerender } = render(plain(RACES));

    rerender(plain([]));
    expect(container).toBeEmptyDOMElement();

    rerender(plain(RACES));
    // Показанная карточка и счётчик согласованы: «N из 4» и N-й вариант.
    const position = Number(
      /^(\d+) из 4$/.exec(counter().textContent ?? "")?.[1],
    );
    expect(shown()).toHaveTextContent(RACES[position - 1].name);
  });

  test("клавиши при фокусе на точке листают от показанной карточки, а не от точки", () => {
    renderCarousel("elf");
    const dot = within(carousel()).getByRole("button", { name: "Полурослик" });

    dot.focus();
    fireEvent.keyDown(dot, { key: "ArrowLeft" });

    expect(shown()).toHaveTextContent("Человек");
    expect(counter()).toHaveTextContent("1 из 4");
  });

  test("←/→ отменяют прокрутку страницы; остальные клавиши (↓, Tab, Пробел) не перехватываются", () => {
    renderCarousel();

    expect(fireEvent.keyDown(carousel(), { key: "ArrowRight" })).toBe(false);
    for (const key of ["ArrowDown", "ArrowUp", "Tab", " "]) {
      expect(fireEvent.keyDown(carousel(), { key }), key).toBe(true);
    }
    expect(shown()).toHaveTextContent("Эльф");
  });

  test("отпускание без нажатия в карусели (жест начат снаружи) не листает и щелчок не гасит", () => {
    const onPick = renderCarousel();

    release(carousel(), { clientX: 100, clientY: 100 });
    expect(shown()).toHaveTextContent("Человек");
    fireEvent.click(screen.getByRole("button", { name: "Выбрать" }));

    expect(onPick).toHaveBeenCalledWith("human");
  });

  // D1 (финальное покрытие блока 7, R2 reviewer): прошлый жест мышью ушёл за край карусели, и
  // кнопку отпустили снаружи. Отпускание где угодно завершает жест, поэтому следующее «нажал
  // снаружи — отпустил на карточке» не считается свайпом от старой точки.
  test("D1: жест, начатый вне карусели, не листает её и не гасит щелчок, даже если прошлый ушёл за край", () => {
    const onPick = vi.fn();
    render(
      <>
        <p>Снаружи</p>
        <Carousel label="Раса" items={RACES}>
          {(item) => (
            <button type="button" onClick={() => onPick(item.id)}>
              Выбрать
            </button>
          )}
        </Carousel>
      </>,
    );
    const outside = screen.getByText("Снаружи");

    // Прошлый жест: нажали на карусели, отпустили за её краем.
    press(carousel(), { clientX: 300, clientY: 100 });
    release(outside, { clientX: 300, clientY: 400 });
    // Новый жест: нажали снаружи, отпустили на «Выбрать» в карточке.
    press(outside, { clientX: 100, clientY: 100 });
    const pick = screen.getByRole("button", { name: "Выбрать" });
    release(pick, { clientX: 100, clientY: 100 });
    fireEvent.click(pick);

    expect(counter()).toHaveTextContent("1 из 4");
    expect(onPick).toHaveBeenCalledWith("human");
  });

  test("второй палец не начинает жест и не подменяет точку начала первого", () => {
    renderCarousel("elf");
    const area = carousel();
    const touch = { pointerType: "touch" } as const;
    const second = { ...touch, pointerId: 2, isPrimary: false } as const;

    // Второй палец сам по себе: ни нажатие, ни отпускание не листают.
    press(area, { ...second, clientX: 300, clientY: 100 });
    release(area, { ...second, clientX: 100, clientY: 100 });
    expect(shown()).toHaveTextContent("Эльф");

    // Первый палец стоит на месте, второй касается дальше: жест первого — не свайп.
    press(area, { ...touch, clientX: 200, clientY: 100 });
    press(area, { ...second, clientX: 400, clientY: 100 });
    release(area, { ...second, clientX: 400, clientY: 100 });
    release(area, { ...touch, clientX: 200, clientY: 100 });
    expect(shown()).toHaveTextContent("Эльф");

    // Свайп первым пальцем по-прежнему листает.
    press(area, { ...touch, clientX: 300, clientY: 100 });
    release(area, { ...touch, clientX: 100, clientY: 100 });
    expect(shown()).toHaveTextContent("Дварф");
  });

  test.each([
    ["правая", 2],
    ["средняя", 1],
  ])("%s кнопка мыши не листает", (_name, button) => {
    renderCarousel("elf");
    const area = carousel();

    press(area, { button, clientX: 300, clientY: 100 });
    release(area, { button, clientX: 100, clientY: 100 });
    expect(shown()).toHaveTextContent("Эльф");

    // Прежний жест правой кнопкой не оставил точку начала: левая листает от своей.
    press(area, { clientX: 200, clientY: 100 });
    release(area, { clientX: 210, clientY: 100 });
    expect(shown()).toHaveTextContent("Эльф");
  });

  test("отпускание другого указателя не завершает жест: листает отпускание того же", () => {
    renderCarousel("elf");
    const area = carousel();

    press(area, { clientX: 300, clientY: 100 });
    release(area, { pointerId: 7, clientX: 100, clientY: 100 });
    expect(shown()).toHaveTextContent("Эльф");
    release(area, { clientX: 100, clientY: 100 });
    expect(shown()).toHaveTextContent("Дварф");
  });

  test("после свайпа пальцем щелчка нет — следующее нажатие кнопки срабатывает", async () => {
    const onPick = renderCarousel();

    // Палец: за свайпом щелчок не приходит, гашение не должно «дождаться» чужого нажатия.
    swipe(300, 100);
    expect(shown()).toHaveTextContent("Эльф");
    await new Promise((resolve) => setTimeout(resolve, 0));
    fireEvent.click(screen.getByRole("button", { name: "Выбрать" }));

    expect(onPick).toHaveBeenCalledWith("elf");
  });

  test("вертикальный жест (прокрутка страницы) не гасит щелчок по кнопке карточки", () => {
    const onPick = renderCarousel();

    swipe(300, 250, 200);
    fireEvent.click(screen.getByRole("button", { name: "Выбрать" }));

    expect(shown()).toHaveTextContent("Человек");
    expect(onPick).toHaveBeenCalledWith("human");
  });

  test("свайп, закончившийся на стрелке или точке, листает на одну карточку: щелчок по ним гасится", () => {
    renderCarousel();

    press(arrow("Следующая"), { clientX: 300, clientY: 100 });
    release(arrow("Следующая"), { clientX: 100, clientY: 100 });
    fireEvent.click(arrow("Следующая"));
    expect(shown()).toHaveTextContent("Эльф");

    const dot = within(carousel()).getByRole("button", { name: "Полурослик" });
    press(dot, { clientX: 100, clientY: 100 });
    release(dot, { clientX: 300, clientY: 100 });
    fireEvent.click(dot);
    expect(shown()).toHaveTextContent("Человек");
    expect(counter()).toHaveTextContent("1 из 4");
  });
});

// Мобильная вёрстка (план B8.1a, e2e/layout.ts): область нажатия кнопки не меньше --tap-size
// и по высоте, и по ширине. В jsdom размеры не измерить, поэтому закреплены правила стилей
// для каждой кнопки, которую рисует сама карусель.
describe("Carousel: область нажатия кнопок", () => {
  test("стрелки — обычные ui-button: их размер задаёт общее правило, своё его не уменьшает", () => {
    renderCarousel();

    for (const name of ["Предыдущая", "Следующая"] as const) {
      expect(arrow(name)).toHaveClass("ui-button", "ui-carousel__arrow");
    }
    expect(ruleOf("ui-button")).toMatch(/min-width:\s*var\(--tap-size\)/);
    expect(ruleOf("ui-button")).toMatch(/min-height:\s*var\(--tap-size\)/);
    expect(ruleOf("ui-carousel__arrow")).not.toMatch(
      /(?<![-\w])(?:min-|max-)?(?:width|height)\s*:/,
    );
  });

  test("точки (свои кнопки, не ui-button) — не меньше --tap-size по ширине и высоте", () => {
    renderCarousel();

    // Кнопки самой карусели: не из карточки (её содержимое рисует вызывающий) и не ui-button.
    const own = within(carousel())
      .getAllByRole("button")
      .filter(
        (button) =>
          button.closest('[aria-roledescription="карточка"]') === null &&
          !button.classList.contains("ui-button"),
      );
    expect(own).toHaveLength(RACES.length);
    expect(new Set(own.map((button) => button.className))).toEqual(
      new Set(["ui-carousel__dot"]),
    );
    const dot = ruleOf("ui-carousel__dot");
    expect(dot).toMatch(/min-width:\s*var\(--tap-size\)/);
    expect(dot).toMatch(/min-height:\s*var\(--tap-size\)/);
    // Иных размеров, кроме --tap-size, у точки нет: меньшая ширина или высота не перебьёт минимум.
    for (const [, value] of dot.matchAll(
      /(?<![-\w])(?:min-|max-)?(?:width|height)\s*:\s*([^;]+);/g,
    )) {
      expect(value.trim()).toBe("var(--tap-size)");
    }
  });

  test("точки переносятся на следующую строку, а не раздвигают страницу", () => {
    expect(ruleOf("ui-carousel__dots")).toMatch(/flex-wrap:\s*wrap/);
  });
});

// R3 reviewer: стрелка у края становится недоступной. Если фокус был на ней, он не уходит
// на body, а переходит на противоположную стрелку: клавиши ←/→ продолжают листать.
describe("Carousel: фокус у края", () => {
  test("«Следующая» до последней карточки: фокус на «Предыдущая», ← листает назад", () => {
    renderCarousel();
    arrow("Следующая").focus();

    for (let i = 0; i < 3; i++) fireEvent.click(arrow("Следующая"));

    expect(shown()).toHaveTextContent("Полурослик");
    expect(arrow("Следующая")).toBeDisabled();
    expect(document.activeElement).toBe(arrow("Предыдущая"));
    expect(carousel()).toContainElement(document.activeElement as HTMLElement);
    fireEvent.keyDown(document.activeElement as Element, { key: "ArrowLeft" });
    expect(shown()).toHaveTextContent("Дварф");
  });

  test("«Предыдущая» до первой карточки: фокус на «Следующая»", () => {
    renderCarousel("dwarf");
    arrow("Предыдущая").focus();

    fireEvent.click(arrow("Предыдущая"));
    fireEvent.click(arrow("Предыдущая"));

    expect(shown()).toHaveTextContent("Человек");
    expect(document.activeElement).toBe(arrow("Следующая"));
  });

  test("клавиша → с фокусом на «Следующая» до края: фокус остаётся в карусели", () => {
    renderCarousel("elf");
    arrow("Следующая").focus();

    fireEvent.keyDown(arrow("Следующая"), { key: "ArrowRight" });
    fireEvent.keyDown(document.activeElement as Element, { key: "ArrowRight" });

    expect(shown()).toHaveTextContent("Полурослик");
    expect(document.activeElement).toBe(arrow("Предыдущая"));
  });

  test("не у края фокус остаётся на нажатой стрелке; с точки фокус не уводится", () => {
    renderCarousel();
    arrow("Следующая").focus();

    fireEvent.click(arrow("Следующая"));
    expect(document.activeElement).toBe(arrow("Следующая"));

    const dot = within(carousel()).getByRole("button", { name: "Полурослик" });
    dot.focus();
    fireEvent.click(dot);
    expect(document.activeElement).toBe(dot);
  });
});
