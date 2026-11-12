import { useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  ExternalLink,
  RefreshCw,
  Sparkles,
  ShieldCheck,
  Code2,
  BookOpen,
  FolderGit2,
  FileCode2,
  Cpu,
  Database,
  Lock,
} from "lucide-react";

import { APP_VERSION } from "@/lib/appVersion";
import { useI18n } from "@/lib/i18n";
import { cn } from "@/lib/utils";

const RELEASES_URL = "https://github.com/demonsxxxxxx/library/releases";
const LATEST_RELEASE_API =
  "https://api.github.com/repos/demonsxxxxxx/library/releases/latest";

interface LatestRelease {
  tag_name?: string;
  name?: string;
  html_url?: string;
  published_at?: string;
}

type CheckState =
  | { status: "idle" }
  | { status: "checking" }
  | { status: "ok"; release: LatestRelease; updateAvailable: boolean }
  | { status: "error"; message: string };

export function AboutPage() {
  const { t } = useI18n();
  const [state, setState] = useState<CheckState>({ status: "idle" });

  const checkLatest = async () => {
    setState({ status: "checking" });
    try {
      const res = await fetch(LATEST_RELEASE_API, {
        headers: { Accept: "application/vnd.github+json" },
      });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const release = (await res.json()) as LatestRelease;
      const latest = normalizeVersion(release.tag_name || release.name || "");
      if (!latest) throw new Error(t.about.latestInvalid);
      setState({
        status: "ok",
        release,
        updateAvailable: compareVersions(latest, normalizeVersion(APP_VERSION)) > 0,
      });
    } catch (e: unknown) {
      setState({
        status: "error",
        message: e instanceof Error ? e.message : String(e),
      });
    }
  };

  return (
    <div className="h-full overflow-y-auto px-6 py-8">
      <div className="mx-auto max-w-3xl space-y-6">
        {/* Hero Card */}
        <div className="relative overflow-hidden rounded-2xl border border-border/80 bg-gradient-to-br from-bg-card via-bg-card to-accent/5 p-6 sm:p-8 shadow-xs">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-6">
            <div className="flex items-center gap-4">
              <div className="relative flex h-16 w-16 shrink-0 items-center justify-center rounded-2xl bg-gradient-to-br from-indigo-500 via-indigo-600 to-indigo-700 text-white shadow-lg shadow-indigo-500/25 ring-1 ring-white/20">
                <BookOpen size={30} strokeWidth={2.2} className="drop-shadow-xs" />
                <div className="absolute -top-1 -right-1 flex h-5 w-5 items-center justify-center rounded-full bg-bg-card text-accent border border-accent/20">
                  <Sparkles size={11} />
                </div>
              </div>
              <div>
                <div className="flex items-center gap-2.5">
                  <h1 className="text-2xl font-bold tracking-tight text-fg-base">
                    Library
                  </h1>
                  <span className="rounded-full bg-accent/10 px-3 py-0.5 font-mono text-xs font-bold text-accent border border-accent/20">
                    v{APP_VERSION}
                  </span>
                </div>
                <p className="mt-1.5 text-xs text-fg-muted">
                  {t.about.subtitle || "Personal AI knowledge workbench with local storage and multimodal retrieval."}
                </p>
              </div>
            </div>

            <button
              type="button"
              disabled={state.status === "checking"}
              onClick={() => void checkLatest()}
              className={cn(
                "inline-flex h-11 items-center justify-center gap-2 rounded-xl bg-accent px-5 text-xs font-semibold text-accent-fg shadow-xs",
                "hover:bg-accent-hover active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-60 transition-all shadow-indigo-500/20",
              )}
            >
              <RefreshCw
                className={cn("h-3.5 w-3.5", state.status === "checking" && "animate-spin")}
              />
              <span>
                {state.status === "checking"
                  ? t.about.checkingLatest
                  : t.about.checkLatest}
              </span>
            </button>
          </div>

          <LatestVersionResult state={state} />
        </div>

        {/* Resources Grid — min-h-[56px] cards */}
        <section className="rounded-2xl border border-border/80 bg-bg-card p-6 sm:p-7 shadow-xs">
          <h2 className="text-sm font-bold text-fg-base tracking-tight mb-4">
            {t.about.linksTitle}
          </h2>
          <div className="grid gap-3.5 sm:grid-cols-2">
            <ExternalLinkButton
              icon={FolderGit2}
              href="https://github.com/demonsxxxxxx/library"
              title={t.about.projectHomepage}
              subtitle="Source code & repository"
            />
            <ExternalLinkButton
              icon={Sparkles}
              href={RELEASES_URL}
              title={t.about.downloadReleases}
              subtitle="Installers & release notes"
            />
            <ExternalLinkButton
              icon={Code2}
              href="https://github.com/demonsxxxxxx/library/issues"
              title={t.about.reportIssue}
              subtitle="Bug reports & feature requests"
            />
            <ExternalLinkButton
              icon={FileCode2}
              href="https://github.com/demonsxxxxxx/library/blob/main/LICENSE"
              title={t.about.license}
              subtitle="Open Source MIT License"
            />
          </div>
        </section>

        {/* Privacy & Local Architecture */}
        <section className="rounded-2xl border border-border/80 bg-bg-card p-6 sm:p-7 shadow-xs">
          <div className="flex items-center gap-3 mb-3.5">
            <div className="flex h-8 w-8 items-center justify-center rounded-xl bg-accent/10 text-accent border border-accent/20 shadow-xs">
              <ShieldCheck size={18} />
            </div>
            <h2 className="text-sm font-bold text-fg-base tracking-tight">
              {t.about.privacyTitle}
            </h2>
          </div>
          <p className="text-xs leading-relaxed text-fg-muted pl-11">
            {t.about.privacyBody}
          </p>

          <div className="mt-5 grid grid-cols-1 sm:grid-cols-3 gap-3 pt-4 border-t border-border/60">
            <div className="flex h-11 items-center gap-2.5 rounded-xl border border-border/60 bg-bg-subtle/40 px-3.5 text-xs">
              <Lock size={15} className="text-accent shrink-0" />
              <span className="font-semibold text-fg-base">Local-First Storage</span>
            </div>
            <div className="flex h-11 items-center gap-2.5 rounded-xl border border-border/60 bg-bg-subtle/40 px-3.5 text-xs">
              <Database size={15} className="text-accent shrink-0" />
              <span className="font-semibold text-fg-base">SQLite & Vector DB</span>
            </div>
            <div className="flex h-11 items-center gap-2.5 rounded-xl border border-border/60 bg-bg-subtle/40 px-3.5 text-xs">
              <Cpu size={15} className="text-accent shrink-0" />
              <span className="font-semibold text-fg-base">Direct LLM Calling</span>
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}

function LatestVersionResult({ state }: { state: CheckState }) {
  const { t, localeTag } = useI18n();
  if (state.status === "idle" || state.status === "checking") {
    return (
      <div className="mt-5 pt-4 border-t border-border/60 text-xs text-fg-subtle">
        {t.about.latestIdle}
      </div>
    );
  }
  if (state.status === "error") {
    return (
      <div className="mt-5 flex items-start gap-3 rounded-2xl border border-danger/20 bg-danger/10 p-4 text-xs text-danger">
        <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-danger" />
        <div>
          <div className="font-bold text-fg-base">{t.about.latestFailed}</div>
          <p className="mt-0.5 text-fg-muted">{state.message}</p>
        </div>
      </div>
    );
  }

  const tag = state.release.tag_name || state.release.name || "";
  const published = state.release.published_at
    ? new Date(state.release.published_at).toLocaleDateString(localeTag)
    : null;

  return (
    <div className="mt-5 flex items-start gap-3.5 rounded-2xl border border-border/70 bg-bg-card p-4 text-xs shadow-xs">
      <CheckCircle2
        className={cn(
          "mt-0.5 h-4.5 w-4.5 shrink-0",
          state.updateAvailable ? "text-accent" : "text-fg-muted",
        )}
      />
      <div className="flex-1">
        <div className="font-bold text-fg-base text-sm">
          {state.updateAvailable
            ? t.about.updateAvailable(tag)
            : t.about.upToDate(tag)}
        </div>
        {published && (
          <p className="mt-1 text-xs text-fg-muted">{t.about.publishedAt(published)}</p>
        )}
        <a
          href={state.release.html_url || RELEASES_URL}
          target="_blank"
          rel="noreferrer"
          className="mt-2.5 inline-flex h-9 items-center gap-1.5 rounded-xl bg-accent px-4 text-xs font-semibold text-accent-fg hover:bg-accent-hover transition-all active:scale-95 shadow-xs"
        >
          <span>{t.about.openLatestRelease}</span>
          <ExternalLink className="h-3 w-3" />
        </a>
      </div>
    </div>
  );
}

function ExternalLinkButton({
  icon: Icon,
  href,
  title,
  subtitle,
}: {
  icon: React.ElementType;
  href: string;
  title: React.ReactNode;
  subtitle: string;
}) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="group flex min-h-[56px] items-center justify-between gap-3.5 rounded-2xl border border-border/70 bg-bg-subtle/40 p-4 transition-all duration-200 hover:-translate-y-0.5 hover:border-accent/40 hover:bg-bg-subtle/70 hover:shadow-xs active:scale-[0.99]"
    >
      <div className="flex items-center gap-3.5 min-w-0">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-bg-card border border-border/60 text-fg-muted group-hover:text-accent group-hover:border-accent/30 transition-colors shadow-xs">
          <Icon size={17} />
        </div>
        <div className="min-w-0">
          <div className="text-xs font-bold text-fg-base group-hover:text-accent transition-colors truncate">
            {title}
          </div>
          <div className="text-[11px] text-fg-subtle truncate mt-0.5">
            {subtitle}
          </div>
        </div>
      </div>
      <ExternalLink className="h-4 w-4 shrink-0 text-fg-subtle group-hover:text-accent transition-colors" />
    </a>
  );
}

function normalizeVersion(value: string): string {
  return value.trim().replace(/^v/i, "").split(/[+-]/, 1)[0];
}

function compareVersions(a: string, b: string): number {
  const pa = parseVersionParts(a);
  const pb = parseVersionParts(b);
  for (let i = 0; i < Math.max(pa.length, pb.length); i += 1) {
    const av = pa[i] ?? 0;
    const bv = pb[i] ?? 0;
    if (av !== bv) return av > bv ? 1 : -1;
  }
  return 0;
}

function parseVersionParts(value: string): number[] {
  return value
    .split(".")
    .map((part) => parseInt(part.replace(/\D.*$/, ""), 10))
    .map((part) => (Number.isFinite(part) ? part : 0));
}
