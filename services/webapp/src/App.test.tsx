import { render, screen } from "@testing-library/react";
import App from "./App";

test("показывает название приложения", () => {
  render(<App />);
  expect(screen.getByText("DnD Adventure Bot")).toBeInTheDocument();
});

test("название приложения — заголовок первого уровня", () => {
  render(<App />);
  expect(
    screen.getByRole("heading", { level: 1, name: "DnD Adventure Bot" }),
  ).toBeInTheDocument();
});

// Testing Library сам чистит DOM после каждого теста, только если test/expect
// глобальные (test.globals в vite.config.ts). Тест рендерит App сам и смотрит
// на DOM уже после очистки: onTestFinished срабатывает позже afterEach-хуков,
// поэтому результат не зависит от соседних тестов, порядка и фильтра -t.
test("DOM после теста очищается автоматически", () => {
  render(<App />);
  expect(screen.getByRole("heading")).toBeInTheDocument();

  onTestFinished(() => {
    expect(screen.queryByRole("heading")).not.toBeInTheDocument();
  });
});
