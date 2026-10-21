import { render, screen } from "@testing-library/react";
import { OPTIONS, previewHero } from "./fixtures";
import { HeroSheet } from "./HeroSheet";

const sheet = () => screen.getByRole("region", { name: "Лист героя" });

describe("лист героя", () => {
  test("названия берутся из правил сервера: класс, вид, предыстория, характеристики", () => {
    render(<HeroSheet character={previewHero()} options={OPTIONS} />);

    expect(screen.getByRole("heading", { name: "Плут" })).toBeInTheDocument();
    expect(sheet()).toHaveTextContent("Эльф · Преступник · уровень 3");
    for (const name of [
      "Сила",
      "Ловкость",
      "Телосложение",
      "Интеллект",
      "Мудрость",
      "Харизма",
    ]) {
      expect(sheet()).toHaveTextContent(name);
    }
    expect(sheet()).toHaveTextContent("Спасброски");
  });

  test("значения характеристик показаны ровно такими, как пришли", () => {
    render(<HeroSheet character={previewHero()} options={OPTIONS} />);

    const term = screen.getByText("Ловкость");
    expect(term.nextElementSibling).toHaveTextContent("16");
    expect(screen.getByText("Сила").nextElementSibling).toHaveTextContent("8");
  });

  test("компетентность отмечена словами", () => {
    render(<HeroSheet character={previewHero()} options={OPTIONS} />);

    expect(sheet()).toHaveTextContent("Скрытность (компетентность)");
    expect(sheet()).toHaveTextContent("Ловкость рук");
    expect(sheet()).not.toHaveTextContent("Ловкость рук (компетентность)");
  });

  test("заклинания, боевой стиль и снаряжение показаны названиями", () => {
    render(
      <HeroSheet
        character={previewHero({
          class_id: "fighter",
          fighting_style: "archery",
          cantrips: ["light"],
          prepared_spells: ["shield"],
          inventory: [
            { item_id: "chain_mail", qty: 1, equipped: true },
            { item_id: "javelin", qty: 4, equipped: false },
          ],
        })}
        options={OPTIONS}
      />,
    );

    expect(sheet()).toHaveTextContent("Стрельба");
    expect(sheet()).toHaveTextContent("Свет");
    expect(sheet()).toHaveTextContent("Щит");
    expect(sheet()).toHaveTextContent("Кольчуга");
    expect(sheet()).toHaveTextContent("Метательное копьё × 4");
  });

  test("пустые разделы не показываются", () => {
    render(<HeroSheet character={previewHero()} options={OPTIONS} />);

    expect(screen.queryByText("Заговоры")).toBeNull();
    expect(screen.queryByText("Заклинания")).toBeNull();
    expect(screen.queryByText("Боевой стиль")).toBeNull();
  });

  test("неизвестный сервером id показывается как есть, а не пропадает", () => {
    render(
      <HeroSheet
        character={previewHero({
          class_id: "bard",
          species: "tabaxi",
          background: "hermit",
          skill_proficiencies: ["perception"],
          inventory: [{ item_id: "lute", qty: 1, equipped: false }],
        })}
        options={OPTIONS}
      />,
    );

    expect(screen.getByRole("heading", { name: "bard" })).toBeInTheDocument();
    expect(sheet()).toHaveTextContent("tabaxi · hermit");
    expect(sheet()).toHaveTextContent("lute");
  });

  test("здоровье показано шкалой с числами сервера", () => {
    render(<HeroSheet character={previewHero()} options={OPTIONS} />);

    expect(screen.getByRole("meter", { name: "Здоровье" })).toHaveAttribute(
      "aria-valuetext",
      "21 из 21",
    );
  });

  test("имени героя в листе нет: в предпросмотре оно служебное", () => {
    render(<HeroSheet character={previewHero()} options={OPTIONS} />);

    expect(sheet()).not.toHaveTextContent("Безымянный странник");
  });
});
