import App from "../App";
import type { Api } from "../api/client";
import { DraftEditor } from "../screens/authoring/DraftEditor";
import { Settings } from "../ui";
import type { PreviewScreen } from "./gate";
import { PreviewShell } from "./PreviewShell";

interface NowScreenProps {
  screen: PreviewScreen;
  api: Api;
  fetchImpl: typeof fetch;
}

const noop = () => {};

/**
 * «Как есть»: настоящие экраны текущего кода. Почти все открывает сам App — сцена уже положила
 * в хранилище сессию и экран, а сеть и сокет обслуживает подмена превью. Редактор приключения и
 * панель настроек App открывает только кликами, поэтому они показаны в том же каркасе напрямую.
 */
export function NowScreen({ screen, api, fetchImpl }: NowScreenProps) {
  if (screen === "settings") {
    return (
      <PreviewShell api={api}>
        <Settings onClose={noop} />
      </PreviewShell>
    );
  }
  if (screen === "authoring") {
    return (
      <PreviewShell api={api}>
        <DraftEditor draftId="d-salt" onClose={noop} />
      </PreviewShell>
    );
  }
  return (
    <App
      deps={{
        fetchImpl,
        readInitData: () => null,
        readStartParam: () => null,
        setupTelegram: noop,
        watchTelegramTheme: noop,
      }}
    />
  );
}
