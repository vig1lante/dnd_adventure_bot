import { fireEvent, render, screen, within } from "@testing-library/react";
import type { DraftRef } from "../../api/types";
import { RefPicker } from "./RefPicker";

// Справочник черновика: все виды сразу, как его отдаёт сервер (дополнение B5, §6).
const REFS: DraftRef[] = [
  { ref: "deity:umberlee", kind: "deity", name: "Амберли" },
  { ref: "faction:guild", kind: "faction", name: "Гильдия теней" },
  { ref: "npc:marta", kind: "npc", name: "Марта" },
  { ref: "location:crypt", kind: "location", name: "Склеп" },
  { ref: "item:crypt-key", kind: "item", name: "Ключ от склепа" },
  { ref: "encounter:crypt-ghouls", kind: "encounter", name: "crypt-ghouls" },
];

function renderPicker(props: Partial<Parameters<typeof RefPicker>[0]> = {}) {
  const onChange = vi.fn();
  render(
    <RefPicker
      label="Кто"
      placeholder="— выберите —"
      kinds={["npc", "faction", "deity", "location"]}
      refs={{ list: REFS, error: null }}
      value=""
      onChange={onChange}
      {...props}
    />,
  );
  const field = screen.getByRole("combobox", { name: props.label ?? "Кто" });
  const options = () =>
    within(field)
      .getAllByRole("option")
      .map((option) => [
        (option as HTMLOptionElement).value,
        option.textContent,
      ]);
  return { field, options, onChange };
}

describe("RefPicker: выбор ссылки из справочника черновика", () => {
  test("только допустимые виды поля, порядок справочника; подпись — имя и вид", () => {
    const { options } = renderPicker();

    expect(options()).toEqual([
      ["", "— выберите —"],
      ["deity:umberlee", "Амберли (бог)"],
      ["faction:guild", "Гильдия теней (фракция)"],
      ["npc:marta", "Марта (персонаж)"],
      ["location:crypt", "Склеп (локация)"],
    ]);
  });

  test("поле одного вида подписывает варианты просто именем", () => {
    const { options } = renderPicker({
      label: "Бой",
      placeholder: "нет",
      kinds: ["encounter"],
    });

    expect(options()).toEqual([
      ["", "нет"],
      ["encounter:crypt-ghouls", "crypt-ghouls"],
    ]);
  });

  test("выбор отдаёт ссылку `<вид>:<id>`, пустой вариант — пустую строку", () => {
    const { field, onChange } = renderPicker({ value: "npc:marta" });

    fireEvent.change(field, { target: { value: "deity:umberlee" } });
    fireEvent.change(field, { target: { value: "" } });

    expect(onChange.mock.calls).toEqual([["deity:umberlee"], [""]]);
  });

  test("текущей ссылки нет в справочнике (удалена или не сохранена) — она видна, а не подменяется", () => {
    const { field, options } = renderPicker({ value: "npc:ghost" });

    expect(field).toHaveValue("npc:ghost");
    expect(options()).toContainEqual(["npc:ghost", "npc:ghost — нет в списке"]);
  });

  test("сбой справочника — текст под полем, текущее значение на месте", () => {
    renderPicker({
      value: "npc:marta",
      refs: { list: [], error: "Сервер устал." },
    });

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Список ссылок не загрузился: Сервер устал.",
    );
    expect(screen.getByRole("combobox", { name: "Кто" })).toHaveValue(
      "npc:marta",
    );
  });
});
