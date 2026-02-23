import os
import subprocess
import re
import logging
import json
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# Slack API Limits
SLACK_TEXT_LIMIT = 3000  # Block Kitのtextフィールドの最大文字数

# ==========================================
# 1. AI連携・実行管理 (GeminiAgent)
# ==========================================
class GeminiAgent:
    """Gemini CLIとのインターフェースを担当するクラス（フォールバック機能付き）"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__ + ".GeminiAgent")
        
        # モデル設定（優先順位順のリストとして保持）
        self.pro_models = self._get_model_list(
            "GEMINI_PRO_MODEL", 
            "gemini-3-pro-preview", 
            "GEMINI_PRO_FALLBACK_MODEL"
        )
        self.flash_models = self._get_model_list(
            "GEMINI_FLASH_MODEL", 
            "gemini-3-flash-preview", 
            "GEMINI_FLASH_FALLBACK_MODEL"
        )
        
        # 冒頭のノイズ除去用
        self._preamble_patterns = re.compile(
            r"^(I'll |I will |Let me |I need to |I should |Checking |Looking |Reading |Searching |Executing |\[tool:)",
            re.IGNORECASE,
        )

    def _get_model_list(self, primary_env, default_val, fallback_env):
        """環境変数からモデルの優先順位リストを作成する"""
        primary = os.environ.get(primary_env, default_val)
        fallbacks = os.environ.get(fallback_env, "").split(",")
        # 空要素を除去してリスト化
        models = [primary] + [m.strip() for m in fallbacks if m.strip()]
        return models

    def _strip_preamble(self, text: str) -> str:
        """冒頭の思考プロセス行をカットする"""
        lines = text.splitlines()
        first_content_idx = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped: continue
            if not self._preamble_patterns.match(stripped):
                first_content_idx = i
                break
        return "\n".join(lines[first_content_idx:]).strip()

    def _execute_with_fallback(self, models, prompt, cwd=None):
        """
        指定されたモデルリストを順に試行し、成功した結果を返す。
        すべてのモデルが失敗した場合は最後の出力を返す。
        """
        last_stdout = ""
        last_stderr = ""

        for model in models:
            self.logger.info(f"Attempting with model: {model}")
            try:
                process = subprocess.run(
                    ["gemini", "-y", "--model", model, "-p", prompt, "--output-format", "text"],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    check=False
                )
                
                # 成功判定 (returncode 0)
                if process.returncode == 0:
                    return process.stdout.strip(), ""
                
                # 失敗した場合はログを残して次へ
                last_stdout = process.stdout.strip()
                last_stderr = process.stderr.strip()
                self.logger.warning(f"Model {model} failed (code {process.returncode}). Stderr: {last_stderr}")
                
            except Exception as e:
                self.logger.error(f"Unexpected error calling model {model}: {e}")
                last_stderr = str(e)

        # すべて失敗した場合
        return last_stdout, last_stderr

    def summarize(self, raw_text: str, context: str) -> str:
        """
        2ステップ目：Flash系モデルを使用して、生出力を要約させる（フォールバック対応）
        """
        if not raw_text or raw_text.startswith("Error output:"):
            return raw_text

        summary_prompt = (
            f"あなたは優秀なAIコンシェルジュです。以下の生出力（{context}）を読み取り、"
            "開発者がSlackで内容を即座に理解できるように日本語で要約してください。\n\n"
            "【ルール】\n"
            "1. 思考プロセスやツール実行ログなどのノイズは完全に排除する。\n"
            "2. 脆弱性の特定、修正方針、検証結果などの重要なポイントを箇条書きで抽出する。\n"
            "3. 技術的に正確な情報を保ちつつ、丁寧でプロフェッショナルな日本語にする。\n"
            "4. 結論から書き始める。\n\n"
            f"--- 生出力開始 ---\n{raw_text}\n--- 生出力終了 ---"
        )

        stdout, stderr = self._execute_with_fallback(self.flash_models, summary_prompt)
        self.logger.info(f"Summary output: {stdout}")
        self.logger.info(f"Summary error output: {stderr}")
        return stdout if stdout else f"Summarization failed: {stderr}"

    def run(self, prompt: str, context_name: str, cwd: str = None) -> str:
        """
        1ステップ目：Pro系モデルで作業を実行し、結果をFlash系モデルで要約して返す（フォールバック対応）
        """
        # 1. 生データの取得（高性能なProモデル系で実行・調査）
        stdout, stderr = self._execute_with_fallback(self.pro_models, prompt, cwd=cwd)
        self.logger.info(f"Raw output: {stdout}")
        self.logger.info(f"Error output: {stderr}")

        raw_stdout = self._strip_preamble(stdout)
        self.logger.info(f"Raw output: {raw_stdout}")

        if not raw_stdout and stderr:
            return f"Error output:\n{stderr}"
        if not raw_stdout:
            return "(No output from gemini)"

        # 2. 要約の実行（Flash系モデル）
        return self.summarize(raw_stdout, context_name)


# ==========================================
# 2. プロジェクト・Git操作管理 (ProjectManager)
# ==========================================
class ProjectManager:
    """ファイルシステムとGitリポジトリの操作を担当するクラス"""

    def __init__(self, projects_root: str):
        self.projects_root = projects_root
        os.makedirs(self.projects_root, exist_ok=True)
        self.logger = logging.getLogger(__name__ + ".ProjectManager")

    def extract_snyk_project(self, event: dict) -> str:
        """Snykアラートからプロジェクト名を抽出"""
        texts_to_check = [event.get("text", "")]
        for att in event.get("attachments", []):
            texts_to_check.append(att.get("fallback", ""))
            texts_to_check.append(att.get("text", ""))
            
        for text in texts_to_check:
            if not text: continue
            match = re.search(r"Project:\s*(?:<[^>]+\|)?([a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+)", text)
            if match: return match.group(1)
        return ""

    def setup_repository(self, project_name: str) -> str:
        repo_name = project_name.split("/")[-1]
        target_dir = os.path.join(self.projects_root, repo_name)
        if not os.path.exists(target_dir):
            repo_url = f"git@github.com:{project_name}.git" 
            subprocess.run(["git", "clone", repo_url, target_dir], check=True)
        else:
            subprocess.run(["git", "fetch", "origin"], cwd=target_dir, check=True)
        return target_dir

    def get_git_status(self, target_dir: str) -> str:
        try:
            return subprocess.run(["git", "status", "-s"], cwd=target_dir, capture_output=True, text=True).stdout
        except Exception:
            return ""


# ==========================================
# 3. Slack UI・コンテキスト管理 (SlackUIManager)
# ==========================================
class SlackUIManager:
    def __init__(self, client):
        self.client = client
        self.logger = logging.getLogger(__name__ + ".SlackUIManager")

    def safe_truncate(self, text: str, limit: int = SLACK_TEXT_LIMIT) -> str:
        if len(text) <= limit: return text
        suffix = "\n\n... (文字数制限のため以下略)"
        return text[:limit - len(suffix)] + suffix

    def build_thread_context(self, channel: str, thread_ts: str, bot_user_id: str) -> str:
        try:
            result = self.client.conversations_replies(channel=channel, ts=thread_ts, limit=20)
            messages = result.get("messages", [])
            if len(messages) <= 1: return ""
            conversation = []
            for msg in messages:
                msg_text = msg.get("text", "").strip()
                if not msg_text: continue
                user_id = msg.get("user", "")
                if user_id == bot_user_id:
                    if msg_text.startswith("⏳"): continue
                    conversation.append(f"Assistant: {msg_text}")
                else:
                    clean_text = re.sub(r"^!ghost\s+", "", msg_text).strip()
                    if clean_text: conversation.append(f"User: {clean_text}")
            if conversation and conversation[-1].startswith("User:"): conversation.pop()
            return "\n".join(conversation)
        except Exception as e:
            self.logger.warning(f"Failed to fetch thread context: {e}")
            return ""

    def create_approval_blocks(self, plan_result: str, project_name: str, target_dir: str) -> list:
        safe_plan = self.safe_truncate(plan_result)
        action_value = json.dumps({"project": project_name, "dir": target_dir})
        return [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"📋 *修正計画が作成されました:*\n{safe_plan}\n\nこの計画に基づいて、自律的なコード修正を実行してもよろしいですか？"}
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✅ 修正を許可する", "emoji": True},
                        "style": "primary",
                        "action_id": "approve_snyk_fix",
                        "value": action_value
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "❌ キャンセル", "emoji": True},
                        "style": "danger",
                        "action_id": "cancel_workflow"
                    }
                ]
            }
        ]

    def create_commit_blocks(self, project_name: str, target_dir: str) -> list:
        action_value = json.dumps({"project": project_name, "dir": target_dir})
        return [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "🛠️ *修正が完了しました。* 内容を確認し、コミットとプッシュを実行しますか？"}
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "🚀 コミット＆プッシュ", "emoji": True},
                        "style": "primary",
                        "action_id": "approve_commit",
                        "value": action_value
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "💡 あとで自分でやる", "emoji": True},
                        "action_id": "cancel_workflow"
                    }
                ]
            }
        ]


# ==========================================
# 4. Snykワークフロー管理 (SnykWorkflowHandler)
# ==========================================
class SnykWorkflowHandler:
    def __init__(self, app: App, gemini: GeminiAgent, project_mgr: ProjectManager, slack_ui: SlackUIManager):
        self.app = app
        self.gemini = gemini
        self.project_mgr = project_mgr
        self.slack_ui = slack_ui
        self.logger = logging.getLogger(__name__ + ".SnykWorkflowHandler")

        self.app.action("approve_snyk_fix")(self.handle_approve_fix)
        self.app.action("approve_commit")(self.handle_commit_fix)
        self.app.action("cancel_workflow")(self.handle_cancel_workflow)

    def handle_snyk_alert(self, event, say, channel, thread_ts):
        project_name = self.project_mgr.extract_snyk_project(event)
        if not project_name: return
        alert_context = event.get("text", "")
        for att in event.get("attachments", []):
            alert_context += "\n" + att.get("fallback", "")
        
        say(f"🔍 プロジェクト `{project_name}` の調査を開始します。少々お待ちください...", thread_ts=thread_ts)
        
        try:
            target_dir = self.project_mgr.setup_repository(project_name)
            plan_instruction = (
                f"`{target_dir}において、.agent/skills/fix-snyk/SKILL.md` の手順に従い、Snykアラートに対する具体的な修正計画を策定してください。\n\n"
                f"アラート内容:\n{alert_context}"
            )
            plan_result = self.gemini.run(plan_instruction, "Snyk修正計画", cwd=target_dir)
            blocks = self.slack_ui.create_approval_blocks(plan_result, project_name, target_dir)
            self.app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="修正計画の承認待ちです", blocks=blocks)
        except Exception as e:
            self.logger.error(f"Failed during plan phase: {e}")
            say(f"❌ エラーが発生しました: {str(e)}", thread_ts=thread_ts)

    def handle_approve_fix(self, ack, body, say, client):
        ack()
        action = body["actions"][0]
        channel_id = body["channel"]["id"]
        message_ts = body["message"]["ts"]
        thread_ts = body["message"].get("thread_ts", message_ts)
        data = json.loads(action["value"])
        target_dir = data["dir"]

        client.chat_update(channel=channel_id, ts=message_ts, text="🛠️ 修正処理を実行中...", blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "🛠️ *承認されました。* 修正を実行しています..."}}])

        try:
            fix_instruction = "`.agent/skills/fix-snyk/SKILL.md` に基づき、対象ファイルを実際に修正してください。完了したら、何を行ったか詳細に報告してください。"
            fix_result = self.gemini.run(fix_instruction, "Snyk修正作業サマリ", cwd=target_dir)
            git_status = self.project_mgr.get_git_status(target_dir)
            safe_fix_result = self.slack_ui.safe_truncate(fix_result)
            result_text = f"✅ *修正が完了しました！*\n\n{safe_fix_result}\n"
            if git_status:
                result_text += f"\n*変更されたファイル:*\n```\n{git_status}```"
            commit_blocks = self.slack_ui.create_commit_blocks(data["project"], target_dir)
            client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=result_text, blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": result_text}}] + commit_blocks)
        except Exception as e:
            say(f"❌ 修正中にエラーが発生しました: {str(e)}", thread_ts=thread_ts)

    def handle_commit_fix(self, ack, body, say, client):
        ack()
        action = body["actions"][0]
        channel_id = body["channel"]["id"]
        message_ts = body["message"]["ts"]
        thread_ts = body["message"].get("thread_ts", message_ts)
        data = json.loads(action["value"])
        target_dir = data["dir"]

        client.chat_update(channel=channel_id, ts=message_ts, text="🚀 コミット中...", blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "🚀 *承認されました。* 反映作業を行っています..."}}])

        try:
            commit_instruction = "修正内容を適切なメッセージでコミットし、プッシュしてください。"
            commit_result = self.gemini.run(commit_instruction, "Git反映結果", cwd=target_dir)
            say(f"✨ *完了しました！*\n{commit_result}", thread_ts=thread_ts)
        except Exception as e:
            say(f"❌ エラーが発生しました: {str(e)}", thread_ts=thread_ts)

    def handle_cancel_workflow(self, ack, body, client):
        ack()
        channel_id = body["channel"]["id"]
        message_ts = body["message"]["ts"]
        user_id = body["user"]["id"]
        client.chat_update(channel=channel_id, ts=message_ts, text="キャンセルされました", blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f"🚫 <@{user_id}> によって中断されました。"}}] )


# ==========================================
# 5. メインBotアプリケーション (ConciergeBot)
# ==========================================
class ConciergeBot:
    def __init__(self, app: App):
        self.app = app
        self.logger = logging.getLogger(__name__ + ".ConciergeBot")
        self.gemini = GeminiAgent()
        self.project_mgr = ProjectManager(os.environ.get("PROJECTS_ROOT_DIR", "./projects"))
        self.slack_ui = SlackUIManager(app.client)
        self.snyk_handler = SnykWorkflowHandler(self.app, self.gemini, self.project_mgr, self.slack_ui)
        self.snyk_channel_id = os.environ.get("SNYK_CHANNEL_ID", "")
        self._register_listeners()

    def _register_listeners(self):
        self.app.event("message")(self.handle_message_events)

    def handle_message_events(self, event, say, logger):
        text = event.get("text", "")
        channel = event.get("channel")
        thread_ts = event.get("thread_ts", event.get("ts"))
        ghost_match = re.match(r"!ghost\s+(.*)", text)
        if ghost_match:
            self._handle_ghost_command(ghost_match.group(1).strip(), event, say, channel, thread_ts)
            return
        if (
            channel == self.snyk_channel_id
            # テスト時は以下をコメントアウトする必要あり
            and "bot_id" in event
        ):
            self.snyk_handler.handle_snyk_alert(event, say, channel, thread_ts)

    def _handle_ghost_command(self, instruction, event, say, channel, thread_ts):
        bot_user_id = self.app.client.auth_test()["user_id"]
        context_text = self.slack_ui.build_thread_context(channel, thread_ts, bot_user_id)
        full_prompt = f"以下は過去の会話履歴です:\n---\n{context_text}\n---\n\n指示: {instruction}"
        processing_msg = say("⏳ Gemini 処理中...", thread_ts=thread_ts)
        try:
            response_text = self.gemini.run(full_prompt, "チャット回答")
            final_text = f"```\n{response_text}\n```" if "\n" in response_text else response_text
            self.app.client.chat_update(channel=channel, ts=processing_msg["ts"], text=final_text)
        except Exception as e:
            self.app.client.chat_update(channel=channel, ts=processing_msg["ts"], text=f"❌ Error: {str(e)}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    load_dotenv()
    app_token = os.environ.get("SLACK_APP_TOKEN")
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not app_token or not bot_token:
        print("❌ SLACK_APP_TOKEN or SLACK_BOT_TOKEN is not set.")
    else:
        print("🚀 Starting Slack AI Concierge Bot...")
        bolt_app = App(token=bot_token)
        concierge_bot = ConciergeBot(bolt_app)
        handler = SocketModeHandler(bolt_app, app_token)
        handler.start()