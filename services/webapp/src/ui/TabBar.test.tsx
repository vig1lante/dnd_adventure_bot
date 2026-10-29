import { fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { TabBar, TabPanel } from "./TabBar";
import type { TabBarItem } from "./TabBar";
import { tabKeyStep } from "./tabs";

type Id = "story" | "hero" | "journal";

const ITEMS: readonly TabBarItem<Id>[] = [
  { id: "story", label: "Рассказ", icon: "scroll" },
  { id: "hero", label: "Персонаж", icon: "heart" },
  { id: "journal", label: "Дневник", icon: "seal", badge: "2" },
];

/** Панель с панелями содержимого: выбор живёт в состоянии, как на экране игры. */
function Harness({
  items = ITEMS,
  hidden = false,
}: {
  items?: readonly TabBarItem<Id>[];
  hidden?: boolean;
}) {
  const [selected, setSelected] = useState<Id>("story");
  return (
    <>
      <TabBar
        label="Разделы игры"
        idBase="g"
        items={items}
        selected={selected}
        onSelect={setSelected}
        hidden={hidden}
      />
      {items.map((item) => (
        <TabPanel
          key={item.id}
          idBase="g"
          id={item.id}
          selected={item.id === selected}
        >
          {`Содержимое: ${item.label}`}
        </TabPanel>
      ))}
    </>
  );
}

const tab = (name: string) => screen.getByRole("tab", { name });

describe("tabKeyStep: куда ведёт клавиша", () => {
  test.each([
    ["ArrowRight", 0, 1],
    ["ArrowRight", 2, 0], // по кругу
    ["ArrowLeft", 0, 2],
    ["ArrowLeft", 2, 1],
    ["Home", 2, 0],
    ["End", 0, 2],
  ])("%s с вкладки %i → %i", (key, index, next) => {
    expect(tabKeyStep(key, index, 3)).toBe(next);
  });

  test.each(["Enter", "a", "ArrowDown", "Tab", " "])(
    "«%s» вкладку не меняет",
    (key) => {
      expect(tabKeyStep(key, 1, 3)).toBeNull();
    },
  );
});

describe("TabBar: нижняя панель вкладок", () => {
  test("набор вкладок с именем и вкладки в заданном порядке, имена — ровно слова", () => {
    render(<Harness />);

    const bar = screen.getByRole("tablist", { name: "Разделы игры" });
    const tabs = within(bar).getAllByRole("tab");
    expect(tabs).toEqual([tab("Рассказ"), tab("Персонаж"), tab("Дневник")]);
    // Число бейджа не входит ни в имя, ни в текст вкладки: на точные имена опираются e2e.
    expect(tab("Дневник")).toHaveAccessibleName("Дневник");
    expect(tab("Дневник")).toHaveTextContent(/^Дневник$/);
  });

  test("бейдж виден, скрыт от читалки, а читалке вкладка сообщает «есть новое»", () => {
    render(<Harness />);

    const badge = screen.getByText("2");
    expect(badge).toHaveAttribute("aria-hidden", "true");
    expect(tab("Дневник")).toHaveAccessibleDescription("есть новое");
    expect(tab("Персонаж")).not.toHaveAccessibleDescription("есть новое");
  });

  test("у открытой вкладки бейджа нет, даже если его передали", () => {
    render(
      <TabBar
        label="Разделы игры"
        idBase="g"
        items={[
          { id: "story", label: "Рассказ", icon: "scroll", badge: "3" },
          { id: "hero", label: "Персонаж", icon: "heart" },
        ]}
        selected="story"
        onSelect={() => {}}
      />,
    );

    expect(screen.queryByText("3")).toBeNull();
    expect(tab("Рассказ")).not.toHaveAccessibleDescription("есть новое");
  });

  test("выбранная вкладка: aria-selected и единственная в порядке табуляции", () => {
    render(<Harness />);

    expect(tab("Рассказ")).toHaveAttribute("aria-selected", "true");
    expect(tab("Рассказ")).toHaveAttribute("tabindex", "0");
    for (const other of ["Персонаж", "Дневник"]) {
      expect(tab(other)).toHaveAttribute("aria-selected", "false");
      expect(tab(other)).toHaveAttribute("tabindex", "-1");
    }
  });

  test("нажатие выбирает вкладку и показывает её панель", () => {
    render(<Harness />);

    fireEvent.click(tab("Персонаж"));

    expect(tab("Персонаж")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel", { name: "Персонаж" })).toBeVisible();
    expect(screen.getByText("Содержимое: Рассказ")).not.toBeVisible();
  });

  test("панели связаны с вкладками: aria-controls и aria-labelledby", () => {
    render(<Harness />);

    const panel = screen.getByRole("tabpanel", { name: "Рассказ" });
    expect(tab("Рассказ")).toHaveAttribute("aria-controls", panel.id);
  });

  test("стрелки — по кругу, Home и End — к краям; фокус идёт за выбором", () => {
    render(<Harness />);
    tab("Рассказ").focus();

    fireEvent.keyDown(tab("Рассказ"), { key: "ArrowLeft" });
    expect(tab("Дневник")).toHaveAttribute("aria-selected", "true");
    expect(tab("Дневник")).toHaveFocus();

    fireEvent.keyDown(tab("Дневник"), { key: "Home" });
    expect(tab("Рассказ")).toHaveAttribute("aria-selected", "true");
    expect(tab("Рассказ")).toHaveFocus();

    fireEvent.keyDown(tab("Рассказ"), { key: "End" });
    expect(tab("Дневник")).toHaveFocus();

    fireEvent.keyDown(tab("Дневник"), { key: "ArrowRight" });
    expect(tab("Рассказ")).toHaveAttribute("aria-selected", "true");
  });

  test("Home и End не прокручивают страницу: действие браузера отменено", () => {
    render(<Harness />);

    const event = new KeyboardEvent("keydown", {
      key: "End",
      bubbles: true,
      cancelable: true,
    });
    tab("Рассказ").dispatchEvent(event);

    expect(event.defaultPrevented).toBe(true);
  });

  test("спрятанная панель (открыта клавиатура телефона) не видна", () => {
    render(<Harness hidden />);

    expect(
      screen.getByRole("tablist", { name: "Разделы игры", hidden: true }),
    ).not.toBeVisible();
  });

  test("имена и подписи с разметкой остаются текстом", () => {
    render(
      <Harness
        items={[
          { id: "story", label: "<b>Рассказ</b>", icon: "scroll" },
          { id: "hero", label: "Персонаж", icon: "heart" },
          { id: "journal", label: "Дневник", icon: "seal" },
        ]}
      />,
    );

    expect(tab("<b>Рассказ</b>")).toBeInTheDocument();
    expect(document.querySelector(".ui-tabbar b")).toBeNull();
  });
});
