import { useCallback, useState } from "react";
import { useApi } from "../../api/context";
import type { DraftSummary } from "../../api/types";
import { Alert, Button, Card, EmptyState, Loading } from "../../ui";
import { useAction, useLoaded } from "../hooks";
import { DraftEditor } from "./DraftEditor";
import "../screens.css";
import "./authoring.css";

function DraftRow({
  draft,
  onOpen,
}: {
  draft: DraftSummary;
  onOpen: () => void;
}) {
  const title = draft.title || "Без названия";
  return (
    <li className="games-item">
      <div className="games-item__main">
        <span className="games-item__title">{title}</span>
        {draft.adventure_id !== null && (
          <span className="games-item__meta">{draft.adventure_id}</span>
        )}
      </div>
      <Button
        variant="secondary"
        aria-label={`Открыть: ${title}`}
        onClick={onOpen}
      >
        Открыть
      </Button>
    </li>
  );
}

/** Вкладка «Приключения»: черновики автора, «Новое приключение» и редактор черновика. */
export function AdventuresHome() {
  const api = useApi();
  const [openId, setOpenId] = useState<string | null>(null);
  const loadDrafts = useCallback(() => api.listDrafts(), [api]);
  const [drafts, reload] = useLoaded(loadDrafts);
  const { busy, error, run } = useAction();

  if (openId !== null) {
    return (
      <DraftEditor
        draftId={openId}
        onClose={() => {
          setOpenId(null);
          reload();
        }}
      />
    );
  }

  const create = () =>
    run(async () => {
      const draft = await api.createDraft();
      setOpenId(draft.id);
    });

  return (
    <>
      <Card title="Приключения" icon="scroll">
        <p className="screen-muted">
          Черновики ваших приключений: заполните шаги сами или попросите
          нейронку.
        </p>
        {drafts.status === "loading" && (
          <Loading label="Загружаем черновики…" />
        )}
        {drafts.status === "error" && (
          <div className="screen-stack">
            <Alert>{drafts.message}</Alert>
            <div>
              <Button variant="secondary" onClick={reload}>
                Повторить
              </Button>
            </div>
          </div>
        )}
        {drafts.status === "ready" && drafts.data.length === 0 && (
          <EmptyState icon="candle" title="Пока нет приключений">
            Начните новое: замысел, мир, боги, фракции и сюжет.
          </EmptyState>
        )}
        {drafts.status === "ready" && drafts.data.length > 0 && (
          <ul className="games-list">
            {drafts.data.map((draft) => (
              <DraftRow
                key={draft.id}
                draft={draft}
                onOpen={() => setOpenId(draft.id)}
              />
            ))}
          </ul>
        )}
      </Card>
      {error !== null && <Alert>{error}</Alert>}
      <Button icon="scroll" block busy={busy} onClick={() => void create()}>
        Новое приключение
      </Button>
    </>
  );
}
