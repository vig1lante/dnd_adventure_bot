import { render, screen, within } from "@testing-library/react";
import { DetailCard } from ".";

/** Значение факта: термин и следующее за ним описание в списке фактов. */
function factValue(label: string): string | null {
  const term = screen.getByText(label, { selector: "dt" });
  return term.nextElementSibling?.textContent ?? null;
}

describe("DetailCard", () => {
  test("карточка названа заголовком; английское название и описание — если есть", () => {
    render(
      <DetailCard
        title="Дварф"
        subtitle="Dwarf"
        description="Дварфы крепки, как камень их гор."
      />,
    );

    const card = screen.getByRole("article", { name: "Дварф" });
    expect(
      within(card).getByRole("heading", { level: 3, name: "Дварф" }),
    ).toBeInTheDocument();
    expect(within(card).getByText("Dwarf")).toHaveAttribute("lang", "en");
    expect(
      within(card).getByText("Дварфы крепки, как камень их гор."),
    ).toBeInTheDocument();
  });

  test("без английского названия и описания лишних строк нет", () => {
    render(<DetailCard title="Оборона" />);

    const card = screen.getByRole("article", { name: "Оборона" });
    expect(card.querySelectorAll("p")).toHaveLength(0);
  });

  test("строки «факт — значение» списком терминов", () => {
    render(
      <DetailCard
        title="Дварф"
        facts={[
          { label: "Размер", value: "Средний" },
          { label: "Скорость", value: "9 м" },
        ]}
      />,
    );

    expect(screen.getAllByRole("term")).toHaveLength(2);
    expect(factValue("Размер")).toBe("Средний");
    expect(factValue("Скорость")).toBe("9 м");
  });

  test("разделы со списками: у особенности — уровень, у механической — «действует в бою»", () => {
    render(
      <DetailCard
        title="Воин"
        sections={[
          {
            title: "Особенности",
            entries: [
              {
                level: 1,
                name: "Второе дыхание",
                summary: "Восстанавливает здоровье.",
                mechanical: true,
              },
              {
                level: 3,
                name: "Подкласс",
                summary: "Путь воина.",
                mechanical: false,
              },
            ],
          },
        ]}
      />,
    );

    const section = screen.getByRole("region", { name: "Особенности" });
    expect(
      within(section).getByRole("heading", { level: 4, name: "Особенности" }),
    ).toBeInTheDocument();
    const [breath, subclass] = within(section).getAllByRole("listitem");
    expect(breath).toHaveTextContent("Второе дыхание");
    expect(breath).toHaveTextContent("1-й уровень");
    expect(breath).toHaveTextContent("Восстанавливает здоровье.");
    expect(breath).toHaveTextContent("действует в бою");
    expect(subclass).toHaveTextContent("3-й уровень");
    expect(subclass).not.toHaveTextContent("действует в бою");
  });

  test("у записи без уровня уровня нет; пустой раздел не показан", () => {
    render(
      <DetailCard
        title="Эльф"
        sections={[
          {
            title: "Черты",
            entries: [{ name: "Острые чувства", summary: "Внимательность." }],
          },
          { title: "Особенности", entries: [] },
        ]}
      />,
    );

    expect(screen.getByRole("listitem")).not.toHaveTextContent("уровень");
    expect(screen.queryByRole("region", { name: "Особенности" })).toBeNull();
  });

  test("только название: ни фактов, ни разделов, ни списков, ни кнопок", () => {
    render(<DetailCard title="Оборона" facts={[]} sections={[]} />);

    const card = screen.getByRole("article", { name: "Оборона" });
    expect(within(card).queryAllByRole("term")).toHaveLength(0);
    expect(within(card).queryAllByRole("region")).toHaveLength(0);
    expect(within(card).queryAllByRole("list")).toHaveLength(0);
    expect(within(card).queryAllByRole("button")).toHaveLength(0);
    expect(card).toHaveTextContent(/^Оборона$/);
  });

  test("все разделы пусты — ни одного раздела; непустые идут в заданном порядке", () => {
    render(
      <DetailCard
        title="Прислужник"
        sections={[
          { title: "Черты", entries: [] },
          { title: "Черта", entries: [{ name: "Бдительный", summary: "+5." }] },
          { title: "Особенности", entries: [] },
          { title: "Заметки", entries: [{ name: "Обет", summary: "Тишина." }] },
        ]}
      />,
    );

    expect(screen.getAllByRole("region")).toHaveLength(2);
    expect(
      screen.getAllByRole("heading", { level: 4 }).map((h) => h.textContent),
    ).toEqual(["Черта", "Заметки"]);
  });

  test("запись без пометки mechanical — без «действует в бою»", () => {
    render(
      <DetailCard
        title="Преступник"
        sections={[
          {
            title: "Черта",
            entries: [{ name: "Бдительный", summary: "Бонус к инициативе." }],
          },
        ]}
      />,
    );

    const entry = screen.getByRole("listitem");
    expect(entry).toHaveTextContent("Бдительный");
    expect(entry).not.toHaveTextContent("действует в бою");
    expect(entry).not.toHaveTextContent("уровень");
  });

  test("место для действий внизу карточки", () => {
    render(
      <DetailCard
        title="Эльф"
        actions={<button type="button">Выбрать</button>}
      />,
    );

    expect(
      within(screen.getByRole("article", { name: "Эльф" })).getByRole(
        "button",
        { name: "Выбрать" },
      ),
    ).toBeInTheDocument();
  });
});
