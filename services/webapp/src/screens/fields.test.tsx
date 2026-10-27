import { fireEvent, render, screen } from "@testing-library/react";
import {
  CheckList,
  ChoiceList,
  ErrorAlert,
  SelectField,
  Switch,
  TextAreaField,
  TextField,
} from "./fields";
import type { Choice } from "./fields";

describe("TextField", () => {
  test("подпись связана с полем", () => {
    render(<TextField label="Код приглашения" value="" onChange={() => {}} />);

    expect(screen.getByLabelText("Код приглашения")).toBeInTheDocument();
  });

  test("подсказка и ошибка описывают поле, ошибка помечает его недопустимым", () => {
    render(
      <TextField
        label="Модель"
        hint="Можно оставить пустым."
        error="Слишком длинное имя."
        value="x"
        onChange={() => {}}
      />,
    );

    const field = screen.getByLabelText("Модель");
    expect(field).toBeInvalid();
    expect(field).toHaveAccessibleDescription(
      "Можно оставить пустым. Слишком длинное имя.",
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Слишком длинное имя.");
  });

  test("без ошибки поле допустимо и сообщения об ошибке нет", () => {
    render(<TextField label="Модель" value="" onChange={() => {}} />);

    expect(screen.getByLabelText("Модель")).toBeValid();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("изменение текста доходит до обработчика", () => {
    const onChange = vi.fn();
    render(<TextField label="Модель" value="" onChange={onChange} />);

    fireEvent.change(screen.getByLabelText("Модель"), {
      target: { value: "opus" },
    });

    expect(onChange).toHaveBeenCalledTimes(1);
  });

  test("два поля не путают подписи", () => {
    render(
      <>
        <TextField label="Первое" value="1" onChange={() => {}} />
        <TextField label="Второе" value="2" onChange={() => {}} />
      </>,
    );

    expect(screen.getByLabelText("Первое")).toHaveValue("1");
    expect(screen.getByLabelText("Второе")).toHaveValue("2");
  });
});

describe("ChoiceList", () => {
  const choices: Choice[] = [
    { value: "a", label: "Склеп", description: "Старое кладбище." },
    { value: "b", label: "Башня", disabled: true, description: "Закрыта." },
  ];

  test("группа с названием и по радиокнопке на вариант", () => {
    render(
      <ChoiceList
        legend="Приключение"
        name="adventure"
        choices={choices}
        value="a"
        onChange={() => {}}
      />,
    );

    const group = screen.getByRole("radiogroup", { name: "Приключение" });
    expect(group).toBeInTheDocument();
    expect(screen.getAllByRole("radio")).toHaveLength(2);
    expect(screen.getByRole("radio", { name: /Склеп/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /Башня/ })).not.toBeChecked();
  });

  test("описание входит в доступное имя варианта", () => {
    render(
      <ChoiceList
        legend="Приключение"
        name="adventure"
        choices={choices}
        value="a"
        onChange={() => {}}
      />,
    );

    expect(
      screen.getByRole("radio", { name: /Старое кладбище/ }),
    ).toBeInTheDocument();
  });

  test("выбор доступного варианта сообщает его значение", () => {
    const onChange = vi.fn();
    render(
      <ChoiceList
        legend="Приключение"
        name="adventure"
        choices={[
          { value: "a", label: "Склеп" },
          { value: "c", label: "Лес" },
        ]}
        value="a"
        onChange={onChange}
      />,
    );

    fireEvent.click(screen.getByRole("radio", { name: "Лес" }));

    expect(onChange).toHaveBeenCalledWith("c");
  });

  test("недоступный вариант не выбрать", () => {
    const onChange = vi.fn();
    render(
      <ChoiceList
        legend="Приключение"
        name="adventure"
        choices={choices}
        value="a"
        onChange={onChange}
      />,
    );

    const locked = screen.getByRole("radio", { name: /Башня/ });
    expect(locked).toBeDisabled();
    fireEvent.click(locked);

    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("Switch", () => {
  test("переключатель с подписью и состоянием", () => {
    render(<Switch label="Готов" checked={true} onChange={() => {}} />);

    expect(screen.getByRole("switch", { name: "Готов" })).toBeChecked();
  });

  test("нажатие сообщает новое состояние", () => {
    const onChange = vi.fn();
    render(<Switch label="Готов" checked={false} onChange={onChange} />);

    fireEvent.click(screen.getByRole("switch", { name: "Готов" }));

    expect(onChange).toHaveBeenCalledWith(true);
  });

  test("недоступный переключатель не реагирует, подсказка описывает его", () => {
    const onChange = vi.fn();
    render(
      <Switch
        label="Готов"
        hint="Сначала создайте героя."
        checked={false}
        disabled
        onChange={onChange}
      />,
    );

    const toggle = screen.getByRole("switch", { name: "Готов" });
    expect(toggle).toBeDisabled();
    expect(toggle).toHaveAccessibleDescription("Сначала создайте героя.");
    fireEvent.click(toggle);
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("TextAreaField", () => {
  test("подпись, подсказка и ошибка связаны с многострочным полем", () => {
    render(
      <TextAreaField
        label="Внешность"
        hint="До 400 знаков."
        error="Слишком длинно."
        value="x"
        onChange={() => {}}
      />,
    );

    const field = screen.getByLabelText("Внешность");
    expect(field.tagName).toBe("TEXTAREA");
    expect(field).toBeInvalid();
    expect(field).toHaveAccessibleDescription("До 400 знаков. Слишком длинно.");
    expect(screen.getByRole("alert")).toHaveTextContent("Слишком длинно.");
  });

  test("изменение текста доходит до обработчика", () => {
    const onChange = vi.fn();
    render(<TextAreaField label="Внешность" value="" onChange={onChange} />);

    fireEvent.change(screen.getByLabelText("Внешность"), {
      target: { value: "Высокая" },
    });

    expect(onChange).toHaveBeenCalledTimes(1);
  });
});

describe("SelectField", () => {
  const options = [
    { value: "fighter", label: "Воин" },
    { value: "rogue", label: "Плут" },
  ];

  test("подпись связана со списком, пустое значение показывает подсказку выбора", () => {
    render(
      <SelectField
        label="Класс"
        placeholder="Выберите класс"
        options={options}
        value=""
        onChange={() => {}}
      />,
    );

    const select = screen.getByLabelText("Класс");
    expect(select).toHaveValue("");
    expect(
      screen.getByRole("option", { name: "Выберите класс" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("option")).toHaveLength(3);
  });

  test("выбор варианта сообщает его значение", () => {
    const onChange = vi.fn();
    render(
      <SelectField
        label="Класс"
        options={options}
        value="fighter"
        onChange={onChange}
      />,
    );

    fireEvent.change(screen.getByLabelText("Класс"), {
      target: { value: "rogue" },
    });

    expect(onChange).toHaveBeenCalledWith("rogue");
  });

  test("подсказка и ошибка описывают список", () => {
    render(
      <SelectField
        label="Класс"
        hint="Один на выбор."
        error="Не выбран."
        options={options}
        value=""
        onChange={() => {}}
      />,
    );

    expect(screen.getByLabelText("Класс")).toBeInvalid();
    expect(screen.getByLabelText("Класс")).toHaveAccessibleDescription(
      "Один на выбор. Не выбран.",
    );
  });
});

describe("CheckList", () => {
  const choices: Choice[] = [
    { value: "a", label: "Скрытность" },
    { value: "b", label: "Обман", description: "Ловко врать." },
    { value: "c", label: "Анализ" },
    { value: "d", label: "Магия", disabled: true },
  ];

  const renderList = (values: string[], max?: number, onChange = vi.fn()) => {
    render(
      <CheckList
        legend="Навыки"
        name="skills"
        choices={choices}
        values={values}
        max={max}
        onChange={onChange}
      />,
    );
    return onChange;
  };

  test("группа с названием и по флажку на вариант, отмеченные видны", () => {
    renderList(["b"]);

    expect(screen.getByRole("group", { name: "Навыки" })).toBeInTheDocument();
    expect(screen.getAllByRole("checkbox")).toHaveLength(4);
    expect(screen.getByRole("checkbox", { name: /Обман/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Анализ/ })).not.toBeChecked();
  });

  test("отметка добавляет значение, снятие убирает; порядок как в списке вариантов", () => {
    const onChange = renderList(["c"]);

    fireEvent.click(screen.getByRole("checkbox", { name: /Скрытность/ }));
    expect(onChange).toHaveBeenLastCalledWith(["a", "c"]);

    fireEvent.click(screen.getByRole("checkbox", { name: /Анализ/ }));
    expect(onChange).toHaveBeenLastCalledWith([]);
  });

  test("достигнут предел: остальные варианты неактивны, отмеченные можно снять", () => {
    const onChange = renderList(["a", "b"], 2);

    expect(screen.getByRole("checkbox", { name: /Анализ/ })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: /Скрытность/ })).toBeEnabled();
    fireEvent.click(screen.getByRole("checkbox", { name: /Анализ/ }));
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("checkbox", { name: /Обман/ }));
    expect(onChange).toHaveBeenCalledWith(["a"]);
  });

  test("недоступный вариант не отметить", () => {
    const onChange = renderList([]);

    const locked = screen.getByRole("checkbox", { name: /Магия/ });
    expect(locked).toBeDisabled();
    fireEvent.click(locked);

    expect(onChange).not.toHaveBeenCalled();
  });

  test("описание входит в доступное имя варианта", () => {
    renderList([]);

    expect(
      screen.getByRole("checkbox", { name: /Ловко врать/ }),
    ).toBeInTheDocument();
  });
});

// Дополнение варианта (`more`): например, кнопка «Подробнее» с карточкой. Внутри <label> ему
// не место: кнопка там недопустима, а её текст попал бы в имя флажка.
describe("дополнение варианта (more)", () => {
  const withMore: Choice[] = [
    {
      value: "a",
      label: "Скрытность",
      description: "Ловкость",
      more: <button type="button">Подробнее о скрытности</button>,
    },
    { value: "b", label: "Обман" },
  ];

  test("CheckList: дополнение после варианта, вне подписи; щелчок по нему не отмечает", () => {
    const onChange = vi.fn();
    render(
      <CheckList
        legend="Навыки"
        name="skills"
        choices={withMore}
        values={[]}
        onChange={onChange}
      />,
    );

    const box = screen.getByRole("checkbox", { name: /Скрытность/ });
    const more = screen.getByRole("button", { name: "Подробнее о скрытности" });
    expect(box).toHaveAccessibleName(/^Скрытность\s*Ловкость$/);
    expect(more.closest("label")).toBeNull();
    expect(box.closest("label")?.nextElementSibling).toBe(more);
    expect(screen.getByRole("group", { name: "Навыки" })).toContainElement(
      more,
    );

    fireEvent.click(more);
    expect(onChange).not.toHaveBeenCalled();
  });

  test("ChoiceList: дополнение после варианта, вне подписи; щелчок по нему не выбирает", () => {
    const onChange = vi.fn();
    render(
      <ChoiceList
        legend="Навык"
        name="skill"
        choices={withMore}
        value={null}
        onChange={onChange}
      />,
    );

    const radio = screen.getByRole("radio", { name: /Скрытность/ });
    const more = screen.getByRole("button", { name: "Подробнее о скрытности" });
    expect(radio).toHaveAccessibleName(/^Скрытность\s*Ловкость$/);
    expect(more.closest("label")).toBeNull();
    expect(radio.closest("label")?.nextElementSibling).toBe(more);

    fireEvent.click(more);
    expect(onChange).not.toHaveBeenCalled();
  });

  test("без дополнения разметка прежняя: в группе только подписи вариантов", () => {
    render(
      <CheckList
        legend="Навыки"
        name="skills"
        choices={[{ value: "b", label: "Обман" }]}
        values={[]}
        onChange={() => {}}
      />,
    );

    const group = screen.getByRole("group", { name: "Навыки" });
    expect(
      [...group.children].map((child) => child.tagName.toLowerCase()),
    ).toEqual(["legend", "label"]);
  });
});

// Финальное покрытие блока 7: пограничные случаи дополнения варианта.
describe("дополнение варианта (more): пограничные случаи", () => {
  test("ChoiceList без дополнения: разметка прежняя, в группе только подписи вариантов", () => {
    render(
      <ChoiceList
        legend="Форма бонуса"
        name="shape"
        choices={[
          { value: "0", label: "+2, +1" },
          { value: "1", label: "+1, +1, +1" },
        ]}
        value="0"
        onChange={() => {}}
      />,
    );

    const group = screen.getByRole("radiogroup", { name: "Форма бонуса" });
    expect(
      [...group.children].map((child) => child.tagName.toLowerCase()),
    ).toEqual(["legend", "label", "label"]);
  });

  test("пустое дополнение (false, null) ничего не добавляет: разметка как без него", () => {
    render(
      <CheckList
        legend="Навыки"
        name="skills"
        choices={[
          { value: "a", label: "Скрытность", more: false },
          { value: "b", label: "Обман", more: null },
        ]}
        values={[]}
        onChange={() => {}}
      />,
    );

    const group = screen.getByRole("group", { name: "Навыки" });
    expect(
      [...group.children].map((child) => child.tagName.toLowerCase()),
    ).toEqual(["legend", "label", "label"]);
  });

  test("CheckList: у недоступного варианта (набрано до предела) дополнение работает", () => {
    const onMore = vi.fn();
    const onChange = vi.fn();
    render(
      <CheckList
        legend="Навыки: выберите 1"
        name="skills"
        choices={[
          { value: "a", label: "Скрытность" },
          {
            value: "b",
            label: "Обман",
            more: (
              <button type="button" onClick={onMore}>
                Подробнее: Обман
              </button>
            ),
          },
        ]}
        values={["a"]}
        max={1}
        onChange={onChange}
      />,
    );

    expect(screen.getByRole("checkbox", { name: "Обман" })).toBeDisabled();
    const more = screen.getByRole("button", { name: "Подробнее: Обман" });
    expect(more).toBeEnabled();
    fireEvent.click(more);

    expect(onMore).toHaveBeenCalledTimes(1);
    expect(onChange).not.toHaveBeenCalled();
  });

  test("ChoiceList: у недоступного варианта дополнение работает и выбор не меняет", () => {
    const onMore = vi.fn();
    const onChange = vi.fn();
    render(
      <ChoiceList
        legend="Боевой стиль"
        name="style"
        choices={[
          {
            value: "archery",
            label: "Стрельба",
            disabled: true,
            more: (
              <button type="button" onClick={onMore}>
                Подробнее: Стрельба
              </button>
            ),
          },
        ]}
        value={null}
        onChange={onChange}
      />,
    );

    expect(screen.getByRole("radio", { name: "Стрельба" })).toBeDisabled();
    fireEvent.click(
      screen.getByRole("button", { name: "Подробнее: Стрельба" }),
    );

    expect(onMore).toHaveBeenCalledTimes(1);
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("поля без подсказки и ошибки", () => {
  test("многострочное поле допустимо и ни на что не ссылается", () => {
    render(<TextAreaField label="Внешность" value="" onChange={() => {}} />);

    const field = screen.getByLabelText("Внешность");
    expect(field).toBeValid();
    expect(field).not.toHaveAttribute("aria-describedby");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("список допустим и ни на что не ссылается", () => {
    render(
      <SelectField
        label="Класс"
        options={[{ value: "rogue", label: "Плут" }]}
        value="rogue"
        onChange={() => {}}
      />,
    );

    const select = screen.getByLabelText("Класс");
    expect(select).toBeValid();
    expect(select).not.toHaveAttribute("aria-describedby");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("текстовое поле без подсказки ни на что не ссылается", () => {
    render(<TextField label="Модель" value="" onChange={() => {}} />);

    expect(screen.getByLabelText("Модель")).not.toHaveAttribute(
      "aria-describedby",
    );
  });
});

describe("ErrorAlert", () => {
  test("нет ошибки — ничего не показано, даже если проблемы переданы", () => {
    const { container } = render(
      <ErrorAlert error={null} problems={["Забытая проблема"]} />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  test("отказ без подробностей: только текст сервера, списка нет", () => {
    render(<ErrorAlert error="Игра уже началась." problems={[]} />);

    expect(screen.getByRole("alert")).toHaveTextContent("Игра уже началась.");
    expect(screen.queryByRole("list")).toBeNull();
  });

  test("отказ правил: текст и каждая проблема отдельной строкой списка", () => {
    render(
      <ErrorAlert
        error="Герой не подходит."
        problems={["Класс не выбран", "Навыков должно быть два"]}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("Герой не подходит.");
    expect(
      screen.getAllByRole("listitem").map((item) => item.textContent),
    ).toEqual(["Класс не выбран", "Навыков должно быть два"]);
  });

  test("одинаковые проблемы не ломают список: ключи уникальны, предупреждений React нет", () => {
    const warn = vi.spyOn(console, "error").mockImplementation(() => {});

    render(
      <ErrorAlert
        error="Герой не подходит."
        problems={["Навык выбран дважды", "Навык выбран дважды"]}
      />,
    );

    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(warn).not.toHaveBeenCalled();
  });

  test("текст выводится буквами, а не разметкой", () => {
    const { container } = render(
      <ErrorAlert error="<b>Сбой</b>" problems={["<i>Проблема</i>"]} />,
    );

    expect(container.querySelector("b")).toBeNull();
    expect(container.querySelector("i")).toBeNull();
    expect(container).toHaveTextContent("<i>Проблема</i>");
  });
});
