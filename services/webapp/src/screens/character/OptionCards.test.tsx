import { fireEvent, render, screen, within } from "@testing-library/react";
import { readSource } from "../../styles/read-source";
import { OPTIONS, OUTDATED_WORDS } from "./fixtures";
import {
  BackgroundCarousel,
  ClassCarousel,
  MoreDetails,
  RaceCarousel,
  SkillCard,
  SpellCard,
  SummaryCard,
} from "./OptionCards";

/** Значение строки «факт — значение» в показанной карточке. */
function fact(label: string): string | null {
  return (
    screen.getByText(label, { selector: "dt" }).nextElementSibling
      ?.textContent ?? null
  );
}

const card = (name: string) => screen.getByRole("article", { name });
const next = () =>
  fireEvent.click(screen.getByRole("button", { name: "Следующая" }));
/** Листает точкой к варианту. */
const showCard = (carousel: string, name: string) =>
  fireEvent.click(
    within(screen.getByRole("group", { name: carousel })).getByRole("button", {
      name,
    }),
  );

describe("RaceCarousel", () => {
  test("карусель «Раса»: карточка с размером, скоростью в метрах, описанием и чертами", () => {
    render(<RaceCarousel options={OPTIONS} value={null} onChange={vi.fn()} />);

    expect(screen.getByRole("group", { name: "Раса" })).toHaveAttribute(
      "aria-roledescription",
      "карусель",
    );
    expect(screen.getByText("1 из 4")).toBeInTheDocument();
    const human = card("Человек");
    expect(human).toHaveTextContent("Люди живут почти везде");
    expect(fact("Размер")).toBe("Средний");
    expect(fact("Скорость")).toBe("9 м");
    const traits = within(
      within(human).getByRole("region", { name: "Черты" }),
    ).getAllByRole("listitem");
    expect(traits).toHaveLength(2);
    expect(traits[0]).toHaveTextContent("Находчивость");
    expect(traits[0]).toHaveTextContent("Вдохновение после отдыха.");
    expect(traits[0]).toHaveTextContent("действует в бою");
    expect(traits[1]).toHaveTextContent("Умелость");
    expect(traits[1]).not.toHaveTextContent("действует в бою");
  });

  test("рядом с названием — английское название расы, с языком en", () => {
    render(<RaceCarousel options={OPTIONS} value={null} onChange={vi.fn()} />);

    expect(within(card("Человек")).getByText("Human")).toHaveAttribute(
      "lang",
      "en",
    );
    showCard("Раса", "Полурослик");
    expect(within(card("Полурослик")).getByText("Halfling")).toHaveAttribute(
      "lang",
      "en",
    );
  });

  test("скорость — только из speed_text сервера; маленький размер — его подписью", () => {
    render(<RaceCarousel options={OPTIONS} value={null} onChange={vi.fn()} />);

    showCard("Раса", "Эльф");
    expect(fact("Скорость")).toBe("10,5 м");
    showCard("Раса", "Полурослик");
    expect(fact("Размер")).toBe("Маленький");
  });

  test("во всех карточках рас нет ни футов, ни «вид»", () => {
    render(<RaceCarousel options={OPTIONS} value={null} onChange={vi.fn()} />);

    for (let i = 0; i < OPTIONS.species.length; i++) {
      expect(document.body.textContent).not.toMatch(OUTDATED_WORDS);
      next();
    }
  });

  test("«Выбрать» выбирает показанную расу", () => {
    const onChange = vi.fn();
    render(<RaceCarousel options={OPTIONS} value={null} onChange={onChange} />);

    showCard("Раса", "Дварф");
    const choose = within(card("Дварф")).getByRole("button", {
      name: "Выбрать",
    });
    expect(choose).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(choose);

    expect(onChange).toHaveBeenCalledWith("dwarf");
  });

  test("начинается с выбранной расы: у неё «Выбрано», у соседней — «Выбрать»", () => {
    render(<RaceCarousel options={OPTIONS} value="elf" onChange={vi.fn()} />);

    expect(screen.getByText("2 из 4")).toBeInTheDocument();
    expect(
      within(card("Эльф")).getByRole("button", { name: "Выбрано" }),
    ).toHaveAttribute("aria-pressed", "true");
    next();
    expect(
      within(card("Дварф")).getByRole("button", { name: "Выбрать" }),
    ).toHaveAttribute("aria-pressed", "false");
  });
});

describe("ClassCarousel", () => {
  test("карточка класса: кость хитов, основная характеристика, спасброски, доспехи, оружие, навыки", () => {
    render(<ClassCarousel options={OPTIONS} value={null} onChange={vi.fn()} />);

    expect(screen.getByRole("group", { name: "Класс" })).toBeInTheDocument();
    expect(card("Воин")).toBeInTheDocument();
    expect(fact("Кость хитов")).toBe("к10");
    expect(fact("Основная характеристика")).toBe("Сила");
    expect(fact("Спасброски")).toBe("Сила, Телосложение");
    expect(fact("Доспехи")).toBe(
      "Лёгкие доспехи, Средние доспехи, Тяжёлые доспехи, Щиты",
    );
    expect(fact("Оружие")).toBe("Простое оружие, Воинское оружие");
    expect(fact("Навыки")).toBe(
      "2 на выбор: Акробатика, Атлетика, История, Проницательность, Внимательность, Выживание",
    );
    expect(fact("Магия")).toBe("Нет");
  });

  test("особенности с уровнем и описанием; у механических — «действует в бою»", () => {
    render(
      <ClassCarousel options={OPTIONS} value="rogue" onChange={vi.fn()} />,
    );

    const features = within(
      within(card("Плут")).getByRole("region", { name: "Особенности" }),
    ).getAllByRole("listitem");
    expect(features.map((item) => item.textContent)).toEqual([
      expect.stringMatching(
        /Скрытая атака.*1-й уровень.*действует в бою.*Дополнительный урон\./,
      ),
      expect.stringMatching(/Воровской жаргон.*1-й уровень.*Тайный язык/),
    ]);
    expect(features[1]).not.toHaveTextContent("действует в бою");
  });

  test("особенность второго уровня показана со своим уровнем", () => {
    render(<ClassCarousel options={OPTIONS} value={null} onChange={vi.fn()} />);

    expect(
      screen.getByText("Всплеск действий").closest("li"),
    ).toHaveTextContent("2-й уровень");
  });

  test("волшебник без доспехов, магия — характеристика и числа на уровне героя", () => {
    render(
      <ClassCarousel options={OPTIONS} value="wizard" onChange={vi.fn()} />,
    );

    expect(fact("Доспехи")).toBe("Нет");
    expect(fact("Магия")).toBe("Интеллект; заговоров: 2; заклинаний: 2");
  });

  test("заклинатель без заговоров: заговоров в строке магии нет", () => {
    render(
      <ClassCarousel options={OPTIONS} value="ranger" onChange={vi.fn()} />,
    );

    expect(fact("Магия")).toBe("Мудрость; заклинаний: 2");
  });

  test("«Выбрать» выбирает показанный класс", () => {
    const onChange = vi.fn();
    render(
      <ClassCarousel options={OPTIONS} value={null} onChange={onChange} />,
    );

    showCard("Класс", "Волшебник");
    fireEvent.click(screen.getByRole("button", { name: "Выбрать" }));

    expect(onChange).toHaveBeenCalledWith("wizard");
  });
});

describe("BackgroundCarousel", () => {
  test("карточка предыстории: характеристики, навыки, инструмент, черта с описанием, снаряжение", () => {
    render(
      <BackgroundCarousel options={OPTIONS} value={null} onChange={vi.fn()} />,
    );

    expect(
      screen.getByRole("group", { name: "Предыстория" }),
    ).toBeInTheDocument();
    const criminal = card("Преступник");
    expect(fact("Характеристики для прибавки")).toBe(
      "Ловкость, Телосложение, Интеллект",
    );
    expect(fact("Навыки")).toBe("Ловкость рук, Скрытность");
    expect(fact("Инструмент")).toBe("Воровские инструменты");
    expect(fact("Снаряжение")).toBe("Кинжал × 1");
    const feat = within(
      within(criminal).getByRole("region", { name: "Черта" }),
    ).getByRole("listitem");
    expect(feat).toHaveTextContent("Бдительный");
    expect(feat).toHaveTextContent("Бонус к инициативе.");
  });

  test("«Выбрать» выбирает показанную предысторию", () => {
    const onChange = vi.fn();
    render(
      <BackgroundCarousel options={OPTIONS} value={null} onChange={onChange} />,
    );

    showCard("Предыстория", "Прислужник");
    expect(fact("Инструмент")).toBe("Набор каллиграфа");
    fireEvent.click(screen.getByRole("button", { name: "Выбрать" }));

    expect(onChange).toHaveBeenCalledWith("acolyte");
  });
});

describe("карточки навыка, заклинания, черты и боевого стиля", () => {
  test("навык: характеристика и примеры применения", () => {
    const athletics = OPTIONS.skills.find((item) => item.id === "athletics")!;
    render(<SkillCard skill={athletics} options={OPTIONS} />);

    expect(card("Атлетика")).toHaveTextContent(
      "Сила в деле: лазать, прыгать, плыть.",
    );
    expect(fact("Характеристика")).toBe("Сила");
  });

  test("заклинание: уровень, школа, время, дистанция в метрах, длительность, описание", () => {
    const sleep = OPTIONS.classes
      .find((item) => item.id === "wizard")!
      .spellcasting!.spell_options.find((item) => item.id === "sleep")!;
    render(<SpellCard spell={sleep} />);

    expect(card("Сон")).toHaveTextContent("Усыпляет.");
    expect(fact("Уровень")).toBe("1-й");
    expect(fact("Школа")).toBe("Очарование");
    expect(fact("Время сотворения")).toBe("Действие");
    expect(fact("Дистанция")).toBe("Близко, до 18 м");
    expect(fact("Длительность")).toBe("1 минута, концентрация");
  });

  test("заговор назван заговором, а не нулевым уровнем", () => {
    const light = OPTIONS.classes
      .find((item) => item.id === "wizard")!
      .spellcasting!.cantrip_options.find((item) => item.id === "light")!;
    render(<SpellCard spell={light} />);

    expect(fact("Уровень")).toBe("Заговор");
    expect(fact("Дистанция")).toBe("Касание");
  });

  test("черта и боевой стиль: описание из details", () => {
    render(
      <>
        <SummaryCard item={OPTIONS.feats[0]} />
        <SummaryCard item={OPTIONS.fighting_styles[1]} />
      </>,
    );

    expect(card("Бдительный")).toHaveTextContent("Бонус к инициативе.");
    expect(card("Стрельба")).toHaveTextContent("+2 к дальним атакам.");
  });
});

describe("MoreDetails («Подробнее»)", () => {
  test("свёрнута, пока не нажали; повторное нажатие сворачивает", () => {
    render(
      <MoreDetails name="Атлетика">
        <p>Подробная карточка</p>
      </MoreDetails>,
    );

    const toggle = screen.getByRole("button", { name: "Подробнее: Атлетика" });
    expect(toggle).toHaveTextContent("Подробнее");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Подробная карточка")).toBeNull();

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Подробная карточка")).toBeInTheDocument();

    fireEvent.click(toggle);
    expect(screen.queryByText("Подробная карточка")).toBeNull();
  });
});

// Мобильная вёрстка (e2e/layout.ts): кнопки не меньше --tap-size по ширине и высоте. Кнопки
// карточек — общий ui-button (его min-width и min-height проверяет Carousel.test); здесь
// закреплено, что свои правила экрана этот размер не уменьшают.
describe("область нажатия кнопок карточек", () => {
  test("«Выбрать» и «Подробнее» — ui-button; правило «Подробнее» размер не уменьшает", async () => {
    render(
      <>
        <RaceCarousel options={OPTIONS} value={null} onChange={vi.fn()} />
        <MoreDetails name="Атлетика">
          <p>Подробная карточка</p>
        </MoreDetails>
      </>,
    );

    expect(screen.getByRole("button", { name: "Выбрать" })).toHaveClass(
      "ui-button",
    );
    expect(
      screen.getByRole("button", { name: "Подробнее: Атлетика" }),
    ).toHaveClass("ui-button", "char-more__toggle");
    const css = (
      await readSource("../screens/character/character.css")
    ).replace(/\/\*[\s\S]*?\*\//g, "");
    const toggle =
      /\.char-more__toggle[^{]*\{([^}]*)\}/.exec(css)?.[1] ?? "<нет правила>";
    expect(toggle).toMatch(/min-height:\s*var\(--tap-size\)/);
    for (const [, value] of toggle.matchAll(
      /(?<![-\w])(?:min-|max-)?(?:width|height)\s*:\s*([^;]+);/g,
    )) {
      expect(value.trim()).toBe("var(--tap-size)");
    }
  });
});

// Каждый вариант каждого вида — ровно данные `details` сервера; пометка «действует в бою» —
// ровно у механических записей.

/** Записи раздела карточки: название, уровень (если есть), описание и пометка боя. */
function expectEntries(
  region: HTMLElement,
  entries: readonly {
    name: string;
    summary: string;
    level?: number;
    mechanical?: boolean;
  }[],
) {
  const items = within(region).getAllByRole("listitem");
  expect(items).toHaveLength(entries.length);
  entries.forEach((entry, index) => {
    const item = items[index];
    expect(item).toHaveTextContent(entry.name);
    expect(item).toHaveTextContent(entry.summary);
    if (entry.level === undefined) {
      expect(item).not.toHaveTextContent("уровень");
    } else {
      expect(item).toHaveTextContent(`${entry.level}-й уровень`);
    }
    if (entry.mechanical === true) {
      expect(item, entry.name).toHaveTextContent("действует в бою");
    } else {
      expect(item, entry.name).not.toHaveTextContent("действует в бою");
    }
  });
}

describe("все варианты каждого вида — по details", () => {
  test.each(OPTIONS.species.map((species) => [species.name, species] as const))(
    "раса «%s»: описание, размер, скорость текстом, все черты по порядку",
    (name, species) => {
      render(
        <RaceCarousel options={OPTIONS} value={null} onChange={vi.fn()} />,
      );
      showCard("Раса", name);

      const shownCard = card(name);
      expect(shownCard).toHaveTextContent(species.details.description);
      expect(fact("Размер")).toBe(species.details.size);
      expect(fact("Скорость")).toBe(species.details.speed_text);
      expectEntries(
        within(shownCard).getByRole("region", { name: "Черты" }),
        species.details.traits,
      );
    },
  );

  // Ожидания выписаны явно: так видно, во что превращаются коды характеристик и навыков.
  test.each([
    {
      name: "Плут",
      hit: "к8",
      primary: "Ловкость",
      saves: "Ловкость, Интеллект",
      armor: "Лёгкие доспехи",
      weapons: "Простое оружие",
      skills:
        "4 на выбор: Акробатика, Атлетика, Обман, Проницательность, Анализ, Внимательность, Убеждение, Ловкость рук, Скрытность",
      magic: "Нет",
    },
    {
      name: "Волшебник",
      hit: "к6",
      primary: "Интеллект",
      saves: "Интеллект, Мудрость",
      armor: "Нет",
      weapons: "Простое оружие",
      skills: "2 на выбор: Магия, История, Проницательность, Анализ, Религия",
      magic: "Интеллект; заговоров: 2; заклинаний: 2",
    },
    {
      name: "Следопыт",
      hit: "к10",
      primary: "Ловкость",
      saves: "Сила, Ловкость",
      armor: "Лёгкие доспехи, Средние доспехи, Щиты",
      weapons: "Простое оружие, Воинское оружие",
      skills:
        "3 на выбор: Атлетика, Проницательность, Природа, Внимательность, Скрытность, Выживание",
      magic: "Мудрость; заклинаний: 2",
    },
  ])("класс «$name»: все строки карточки", (expected) => {
    render(<ClassCarousel options={OPTIONS} value={null} onChange={vi.fn()} />);
    showCard("Класс", expected.name);

    expect(card(expected.name)).toBeInTheDocument();
    expect(fact("Кость хитов")).toBe(expected.hit);
    expect(fact("Основная характеристика")).toBe(expected.primary);
    expect(fact("Спасброски")).toBe(expected.saves);
    expect(fact("Доспехи")).toBe(expected.armor);
    expect(fact("Оружие")).toBe(expected.weapons);
    expect(fact("Навыки")).toBe(expected.skills);
    expect(fact("Магия")).toBe(expected.magic);
  });

  test.each(OPTIONS.classes.map((cls) => [cls.name, cls] as const))(
    "класс «%s»: особенности с уровнем и описанием, «действует в бою» только у механических",
    (name, cls) => {
      render(
        <ClassCarousel options={OPTIONS} value={null} onChange={vi.fn()} />,
      );
      showCard("Класс", name);

      expectEntries(
        within(card(name)).getByRole("region", { name: "Особенности" }),
        cls.details.features,
      );
    },
  );

  test("предыстория «Прислужник»: все строки; у черты предыстории нет ни уровня, ни пометки боя", () => {
    render(
      <BackgroundCarousel options={OPTIONS} value={null} onChange={vi.fn()} />,
    );
    showCard("Предыстория", "Прислужник");

    expect(fact("Характеристики для прибавки")).toBe(
      "Интеллект, Мудрость, Харизма",
    );
    expect(fact("Навыки")).toBe("Проницательность, Религия");
    expect(fact("Инструмент")).toBe("Набор каллиграфа");
    expect(fact("Снаряжение")).toBe("Священный символ × 1");
    expectEntries(
      within(card("Прислужник")).getByRole("region", { name: "Черта" }),
      [{ name: "Посвящённый в магию", summary: "Магия." }],
    );
  });

  test("снаряжение из нескольких предметов — через запятую, с количеством", () => {
    const [criminal] = OPTIONS.backgrounds;
    const options = {
      ...OPTIONS,
      backgrounds: [
        {
          ...criminal,
          details: {
            ...criminal.details,
            equipment: [
              { id: "dagger", name: "Кинжал", qty: 2 },
              { id: "crowbar", name: "Ломик", qty: 1 },
            ],
          },
        },
      ],
    };
    render(
      <BackgroundCarousel options={options} value={null} onChange={vi.fn()} />,
    );

    expect(fact("Снаряжение")).toBe("Кинжал × 2, Ломик × 1");
  });

  const ALL_SPELLS = OPTIONS.classes.flatMap((cls) =>
    cls.spellcasting === null
      ? []
      : [
          ...cls.spellcasting.cantrip_options,
          ...cls.spellcasting.spell_options,
        ],
  );

  test.each(ALL_SPELLS.map((spell) => [spell.name, spell] as const))(
    "заклинание «%s»: школа, время, дистанция и длительность — строки сервера как есть",
    (name, spell) => {
      render(<SpellCard spell={spell} />);

      expect(card(name)).toHaveTextContent(spell.details.summary);
      expect(fact("Уровень")).toBe(
        spell.level === 0 ? "Заговор" : `${spell.level}-й`,
      );
      expect(fact("Школа")).toBe(spell.details.school);
      expect(fact("Время сотворения")).toBe(spell.details.casting_time);
      expect(fact("Дистанция")).toBe(spell.details.range_text);
      expect(fact("Длительность")).toBe(spell.details.duration);
    },
  );

  test("в заглушках есть все виды времени сотворения и дистанций, какие отдаёт сервер", () => {
    // Иначе проверка выше прошла бы мимо части подписей.
    expect(
      new Set(ALL_SPELLS.map((spell) => spell.details.casting_time)),
    ).toEqual(
      new Set([
        "Действие",
        "Бонусное действие",
        "Несколько минут, только вне боя",
      ]),
    );
    expect(
      new Set(ALL_SPELLS.map((spell) => spell.details.range_text)),
    ).toEqual(
      new Set(["На себя", "Касание", "Близко, до 18 м", "Далеко, дальше 18 м"]),
    );
  });

  test("каждый навык: характеристика его названием и описание с примерами", () => {
    for (const skill of OPTIONS.skills) {
      const { unmount } = render(<SkillCard skill={skill} options={OPTIONS} />);
      const ability = OPTIONS.abilities.find(
        (item) => item.id === skill.details.ability,
      )!;

      expect(card(skill.name)).toHaveTextContent(skill.details.summary);
      expect(fact("Характеристика"), skill.name).toBe(ability.name);
      unmount();
    }
  });

  test("каждая черта и каждый боевой стиль: описание из details", () => {
    for (const item of [...OPTIONS.feats, ...OPTIONS.fighting_styles]) {
      const { unmount } = render(<SummaryCard item={item} />);

      expect(card(item.name)).toHaveTextContent(item.details.summary);
      unmount();
    }
  });

  test("ни на одной карточке предыстории, навыка, заклинания, черты и стиля нет футов и «вид»", () => {
    render(
      <>
        <BackgroundCarousel options={OPTIONS} value={null} onChange={vi.fn()} />
        {OPTIONS.skills.map((skill) => (
          <SkillCard key={skill.id} skill={skill} options={OPTIONS} />
        ))}
        {ALL_SPELLS.map((spell) => (
          <SpellCard key={spell.id} spell={spell} />
        ))}
        {[...OPTIONS.feats, ...OPTIONS.fighting_styles].map((item) => (
          <SummaryCard key={item.id} item={item} />
        ))}
      </>,
    );

    for (let i = 0; i < OPTIONS.backgrounds.length; i++) {
      expect(document.body.textContent).not.toMatch(OUTDATED_WORDS);
      fireEvent.click(
        within(screen.getByRole("group", { name: "Предыстория" })).getByRole(
          "button",
          { name: "Следующая" },
        ),
      );
    }
    expect(document.body.textContent).not.toMatch(OUTDATED_WORDS);
  });
});

describe("карусель выбора: выбор только кнопкой «Выбрать»", () => {
  test("выбранного варианта нет в списке: начинается с первой, «Выбрано» нет ни на одной карточке", () => {
    render(<RaceCarousel options={OPTIONS} value="orc" onChange={vi.fn()} />);

    expect(screen.getByText("1 из 4")).toBeInTheDocument();
    for (let i = 0; i < OPTIONS.species.length; i++) {
      expect(screen.queryByRole("button", { name: "Выбрано" })).toBeNull();
      expect(screen.getByRole("button", { name: "Выбрать" })).toHaveAttribute(
        "aria-pressed",
        "false",
      );
      if (i < OPTIONS.species.length - 1) next();
    }
  });

  test("листание стрелками, точками, клавишами и свайпом ничего не выбирает", async () => {
    const onChange = vi.fn();
    render(
      <ClassCarousel options={OPTIONS} value="rogue" onChange={onChange} />,
    );
    const group = screen.getByRole("group", { name: "Класс" });

    next();
    fireEvent.click(within(group).getByRole("button", { name: "Воин" }));
    fireEvent.keyDown(group, { key: "ArrowRight" });
    // Свайп основным указателем — левой кнопкой мыши (в jsdom isPrimary по умолчанию false).
    const mouse = { pointerId: 1, isPrimary: true, pointerType: "mouse" };
    fireEvent.pointerDown(group, { ...mouse, clientX: 300, clientY: 100 });
    fireEvent.pointerUp(group, { ...mouse, clientX: 100, clientY: 100 });

    expect(onChange).not.toHaveBeenCalled();
    // Выбран по-прежнему плут: показанный волшебник предлагает «Выбрать».
    expect(card("Волшебник")).toBeInTheDocument();
    expect(
      within(card("Волшебник")).getByRole("button", { name: "Выбрать" }),
    ).toHaveAttribute("aria-pressed", "false");
    // Нажатие после свайпа пальцем доходит со следующей задачи (щелчок свайпа гасится).
    await new Promise((resolve) => setTimeout(resolve, 0));
    showCard("Класс", "Плут");
    expect(
      within(card("Плут")).getByRole("button", { name: "Выбрано" }),
    ).toHaveAttribute("aria-pressed", "true");
  });
});
