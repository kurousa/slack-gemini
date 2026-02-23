import os
import subprocess
import re
import logging
import json
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# ==========================================
# 1. AI連携・実行管理 (GeminiAgent)
# ==========================================
class GeminiAgent:
    """Gemini CLIとのインターフェースを担当するクラス"""
    
    def __init__(self):
        # Gemini CLIの思考プロセス部分（YOLOモード時のプレアンブル）を除去する正規表現
        self._preamble_patterns = re.compile(
            r"^(I'll |I will |Let me |I need to |I should |Checking |Looking |Reading |Searching )",
            re.IGNORECASE,
        )

    def strip_preamble(self, text: str) -> str:
        """Gemini CLIの思考/行動宣言部分を削除し、純粋な回答のみを返す"""
        paragraphs = text.split("\n\n")
        while paragraphs:
            first = paragraphs[0].strip()
            if not first or self._preamble_patterns.match(first):
                paragraphs.pop(0)
            else:
                break
        return "\n\n".join(paragraphs).strip() if paragraphs else text.strip()

    def run(self, prompt: str, cwd: str = None) -> str:
        """Gemini CLIを実行し、クリーンな結果を取得する"""
        process = subprocess.run(
            ["gemini", "-y", "-p", prompt, "--output-format", "text"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False
        )
        stdout = process.stdout.strip()
        stderr = process.stderr.strip()
        
        if stdout:
            return self.strip_preamble(stdout)
        elif stderr:
            return f"Error output:\n{stderr}"
        else:
            return "(No output from gemini)"


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
        """Snykのアラートメッセージからプロジェクト名(org/repo)を抽出する"""
        texts_to_check = [event.get("text", "")]
        for att in event.get("attachments", []):
            texts_to_check.append(att.get("fallback", ""))
            texts_to_check.append(att.get("text", ""))
            
        for text in texts_to_check:
            if not text:
                continue
            # 例: "Project: <http://...|kurousa/sql-query-builder:package.json>"
            match = re.search(r"Project:\s*(?:<[^>]+\|)?([a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+)", text)
            if match:
                return match.group(1)
        return ""

    def setup_repository(self, project_name: str) -> str:
        """指定されたプロジェクトを projects/ 以下に準備（存在しなければclone）する"""
        repo_name = project_name.split("/")[-1]
        target_dir = os.path.join(self.projects_root, repo_name)
        
        if not os.path.exists(target_dir):
            self.logger.info(f"Repository {project_name} not found locally. Cloning...")
            repo_url = f"git@github.com:{project_name}.git" 
            try:
                subprocess.run(["git", "clone", repo_url, target_dir], check=True)
                self.logger.info(f"Successfully cloned {project_name}")
            except subprocess.CalledProcessError as e:
                self.logger.error(f"Failed to clone {project_name}: {e}")
                raise Exception(f"Git clone failed: {e}")
        else:
            self.logger.info(f"Repository {project_name} already exists. Fetching latest...")
            try:
                subprocess.run(["git", "fetch", "origin"], cwd=target_dir, check=True)
            except subprocess.CalledProcessError as e:
                self.logger.warning(f"Failed to fetch latest for {project_name}: {e}")
                
        return target_dir

    def get_git_status(self, target_dir: str) -> str:
        """Gitの変更ステータス(diffサマリ)を取得する"""
        try:
            return subprocess.run(["git", "status", "-s"], cwd=target_dir, capture_output=True, text=True).stdout
        except Exception:
            return ""


# ==========================================
# 3. Slack UI・コンテキスト管理 (SlackUIManager)
# ==========================================
class SlackUIManager:
    """Slackのメッセージフォーマット作成や履歴取得を担当するクラス"""

    def __init__(self, client):
        self.client = client
        self.logger = logging.getLogger(__name__ + ".SlackUIManager")

    def build_thread_context(self, channel: str, thread_ts: str, bot_user_id: str) -> str:
        """スレッドの会話履歴を取得し、Gemini用のプロンプト文脈を構築する"""
        try:
            result = self.client.conversations_replies(
                channel=channel,
                ts=thread_ts,
                limit=20,
            )
            messages = result.get("messages", [])
            if len(messages) <= 1:
                return ""
            
            conversation = []
            for msg in messages:
                msg_text = msg.get("text", "").strip()
                if not msg_text:
                    continue
                
                user_id = msg.get("user", "")
                if user_id == bot_user_id:
                    if msg_text.startswith("⏳"):
                        continue
                    conversation.append(f"Assistant: {msg_text}")
                else:
                    clean_text = re.sub(r"^!ghost\s+", "", msg_text).strip()
                    if clean_text:
                        conversation.append(f"User: {clean_text}")
            
            if not conversation:
                return ""
            
            # 直近のユーザーの指示は本プロンプトとして扱うため履歴から除外
            if conversation and conversation[-1].startswith("User:"):
                conversation.pop()
            
            return "\n".join(conversation) if conversation else ""
            
        except Exception as e:
            self.logger.warning(f"Failed to fetch thread context: {e}")
            return ""

    def create_approval_blocks(self, plan_result: str, project_name: str, target_dir: str) -> list:
        """修正計画の承認待ちUI（Block Kit）を生成する"""
        action_value = json.dumps({"project": project_name, "dir": target_dir})
        return [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"📋 *修正計画が作成されました:*\n```\n{plan_result}\n```\n\nこの計画に基づいて、自律的なコード修正を実行してもよろしいですか？"}
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
                        "action_id": "cancel_snyk_fix"
                    }
                ]
            }
        ]


# ==========================================
# 4. Snykワークフロー管理 (SnykWorkflowHandler)
# ==========================================
class SnykWorkflowHandler:
    """Snykのセキュリティアラート検知から修正・承認までの一連のワークフローを担当するクラス"""

    def __init__(self, app: App, gemini: GeminiAgent, project_mgr: ProjectManager, slack_ui: SlackUIManager):
        self.app = app
        self.gemini = gemini
        self.project_mgr = project_mgr
        self.slack_ui = slack_ui
        self.logger = logging.getLogger(__name__ + ".SnykWorkflowHandler")

        # Snyk用のアクションリスナーを登録
        self.app.action("approve_snyk_fix")(self.handle_approve_fix)
        self.app.action("cancel_snyk_fix")(self.handle_cancel_fix)

    def handle_snyk_alert(self, event, say, channel, thread_ts):
        """Snykのアラート検知と修正計画立案ワークフロー"""
        project_name = self.project_mgr.extract_snyk_project(event)
        if not project_name:
            return

        self.logger.info(f"Snyk alert detected for project: {project_name}")
        
        alert_context = event.get("text", "")
        for att in event.get("attachments", []):
            alert_context += "\n" + att.get("fallback", "")
        
        say(f"🔍 プロジェクト `{project_name}` の脆弱性を検知しました。AIコンシェルジュが調査を開始します...", thread_ts=thread_ts)
        
        try:
            target_dir = self.project_mgr.setup_repository(project_name)
            
            plan_instruction = (
                f"必ず `.agent/skills/fix-snyk/SKILL.md` (fix-snykスキル) を参照し、その手順に従ってください。\n"
                f"次のセキュリティアラートについて、Snyk CLI等を用いて詳細を調査し、"
                f"どのファイルをどのように書き換えるべきかの「修正方針（Plan）」を日本語で提示してください。"
                f"※この段階ではまだファイルの修正は実行しないでください。\n\nアラート内容:\n{alert_context}"
            )
            
            plan_result = self.gemini.run(plan_instruction, cwd=target_dir)
            blocks = self.slack_ui.create_approval_blocks(plan_result, project_name, target_dir)
            
            self.app.client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="修正計画の承認待ちです",
                blocks=blocks
            )
        except Exception as e:
            self.logger.error(f"Failed during plan phase: {e}")
            say(f"❌ 計画の作成中にエラーが発生しました: {str(e)}", thread_ts=thread_ts)

    def handle_approve_fix(self, ack, body, say, logger, client):
        """「修正を許可する」ボタンのアクション処理"""
        ack()
        
        action = body["actions"][0]
        channel_id = body["channel"]["id"]
        message_ts = body["message"]["ts"]
        thread_ts = body["message"].get("thread_ts", message_ts)
        user_id = body["user"]["id"]
        
        try:
            data = json.loads(action["value"])
            project_name = data["project"]
            target_dir = data["dir"]
        except Exception as e:
            say(f"データのパースに失敗しました: {e}", thread_ts=thread_ts)
            return

        # 元のメッセージからアクションブロック（ボタン）を取り除く
        original_blocks = body["message"].get("blocks", [])
        if len(original_blocks) > 1:
            original_blocks.pop()
        
        original_blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"<@{user_id}> によって承認されました。🛠️ 修正処理を実行中です..."}
        })
        
        client.chat_update(channel=channel_id, ts=message_ts, blocks=original_blocks, text="修正を実行中...")

        try:
            # 実際のコード修正実行（ここでもSKILL.mdを強制参照させる）
            fix_instruction = (
                f"必ず `.agent/skills/fix-snyk/SKILL.md` の手順に従い、"
                f"先ほど提示した修正方針に基づいて対象ファイルを実際に書き換えてください。"
                f"修正完了後、どのような変更を行ったかの要約を出力してください。"
            )
            fix_result = self.gemini.run(fix_instruction, cwd=target_dir)
            git_status = self.project_mgr.get_git_status(target_dir)
            
            result_msg = f"✅ *修正が完了しました！*\n\n*Gemini作業サマリ:*\n```\n{fix_result}\n```\n"
            if git_status:
                result_msg += f"\n*変更されたファイル:*\n```\n{git_status}```\n（※動作確認後、問題なければ `!ghost コミットして` 等でプッシュを指示してください）"
                
            say(result_msg, thread_ts=thread_ts)
        except Exception as e:
            say(f"❌ 修正の実行中にエラーが発生しました: {str(e)}", thread_ts=thread_ts)

    def handle_cancel_fix(self, ack, body, client):
        """「キャンセル」ボタンのアクション処理"""
        ack()
        
        channel_id = body["channel"]["id"]
        message_ts = body["message"]["ts"]
        user_id = body["user"]["id"]
        
        original_blocks = body["message"].get("blocks", [])
        if len(original_blocks) > 1:
            original_blocks.pop()
        
        original_blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"🚫 <@{user_id}> によって修正処理がキャンセルされました。"}
        })
        
        client.chat_update(channel=channel_id, ts=message_ts, blocks=original_blocks, text="キャンセルされました")


# ==========================================
# 5. メインBotアプリケーション (ConciergeBot)
# ==========================================
class ConciergeBot:
    """Slack Boltのイベントをハンドリングし、全体のワークフローを制御するオーケストレーター"""

    def __init__(self, app: App):
        self.app = app
        self.logger = logging.getLogger(__name__ + ".ConciergeBot")
        
        # 各関心事のマネージャを初期化
        self.gemini = GeminiAgent()
        self.project_mgr = ProjectManager(os.environ.get("PROJECTS_ROOT_DIR", "./projects"))
        self.slack_ui = SlackUIManager(app.client)
        
        # Snykワークフローハンドラーの初期化（内部でアクションリスナーが登録される）
        self.snyk_handler = SnykWorkflowHandler(self.app, self.gemini, self.project_mgr, self.slack_ui)

        # 監視対象チャンネル
        self.snyk_channel_id = os.environ.get("SNYK_CHANNEL_ID", "")

        # Slack Bolt イベントの登録 (メッセージのみ)
        self._register_listeners()

    def _register_listeners(self):
        """Slack Boltのイベントリスナーを登録する"""
        self.app.event("message")(self.handle_message_events)

    def handle_message_events(self, event, say, logger):
        """メッセージイベントの総合ルーティング"""
        text = event.get("text", "")
        channel = event.get("channel")
        thread_ts = event.get("thread_ts", event.get("ts"))

        # 1. !ghost コマンドの処理
        ghost_match = re.match(r"!ghost\s+(.*)", text)
        if ghost_match:
            self._handle_ghost_command(ghost_match.group(1).strip(), event, say, channel, thread_ts)
            return

        # 2. Snykアラートの検知処理
        if channel == self.snyk_channel_id and "bot_id" in event:
            self.snyk_handler.handle_snyk_alert(event, say, channel, thread_ts)

    def _handle_ghost_command(self, instruction, event, say, channel, thread_ts):
        """汎用的なチャット・指示（!ghost）のワークフロー"""
        if not instruction:
            say("Please provide an instruction after `!ghost`.", thread_ts=thread_ts)
            return
        
        self.logger.info(f"Received !ghost command: {instruction}")
        
        context_text = ""
        if "thread_ts" in event:
            bot_user_id = self.app.client.auth_test()["user_id"]
            context_text = self.slack_ui.build_thread_context(channel, thread_ts, bot_user_id)

        if context_text:
            full_prompt = f"以下は過去の会話履歴です:\n---\n{context_text}\n---\n\n上記の会話を踏まえて、以下の質問に回答してください:\n{instruction}"
        else:
            full_prompt = instruction

        processing_msg = say("⏳ Gemini 処理中...", thread_ts=thread_ts)
        
        try:
            response_text = self.gemini.run(full_prompt)
            final_text = f"```\n{response_text}\n```" if "\n" in response_text else response_text
            self.app.client.chat_update(channel=channel, ts=processing_msg["ts"], text=final_text)
        except Exception as e:
            self.app.client.chat_update(channel=channel, ts=processing_msg["ts"], text=f"❌ Error: {str(e)}")


# ==========================================
# 6. エントリポイント
# ==========================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    load_dotenv()

    app_token = os.environ.get("SLACK_APP_TOKEN")
    bot_token = os.environ.get("SLACK_BOT_TOKEN")

    if not app_token or not bot_token:
        print("❌ SLACK_APP_TOKEN or SLACK_BOT_TOKEN is not set in .env file.")
    else:
        print("🚀 Starting Slack AI Concierge Bot in Socket Mode...")
        bolt_app = App(token=bot_token)
        
        # メインクラスをインスタンス化
        concierge_bot = ConciergeBot(bolt_app)
        
        handler = SocketModeHandler(bolt_app, app_token)
        handler.start()