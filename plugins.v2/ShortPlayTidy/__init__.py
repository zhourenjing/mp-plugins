import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.plugin import PluginManager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import NotificationType


class ShortPlayTidy(_PluginBase):
    """短剧规整与封面：规整为每剧一文件夹 + 抽帧生成真实剧照 + 写回 115"""

    plugin_name = "短剧规整与封面"
    plugin_desc = "把「/短剧」的扁平视频/STRM 规整成每剧一文件夹，抽帧生成真实剧照 poster.jpg，并可写回 115 对应文件夹"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/movie.png"
    plugin_version = "1.0.0"
    plugin_author = "quanzhange"
    author_url = ""
    plugin_config_prefix = "shortplaytidy_"
    plugin_order = 30
    auth_level = 1

    _enabled = False
    _notify = True
    _onlyonce = False
    _cron = "*/30 * * * *"
    _strm_root = "/media/links/115strm/短剧"
    _do_poster = True
    _upload_115 = True
    _p115_root = "/短剧"
    _scheduler: Optional[BackgroundScheduler] = None

    # ---------- 生命周期 ----------
    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled", False)
            self._notify = config.get("notify", True)
            self._onlyonce = config.get("onlyonce", False)
            self._cron = (config.get("cron") or "*/30 * * * *").strip()
            self._strm_root = (config.get("strm_root") or self._strm_root).strip()
            self._do_poster = config.get("do_poster", True)
            self._upload_115 = config.get("upload_115", True)
            self._p115_root = (config.get("p115_root") or "/短剧").strip()
        self.stop_service()
        if not self._enabled:
            return
        self._scheduler = BackgroundScheduler(timezone=settings.TZ)
        if self._onlyonce:
            self._scheduler.add_job(func=self.tidy, trigger="date",
                                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)),
                                    name="短剧规整立即执行")
            self._onlyonce = False
            self.__update_config()
        if self._cron:
            try:
                self._scheduler.add_job(func=self.tidy, trigger=CronTrigger.from_crontab(self._cron),
                                        name="短剧规整与封面")
            except Exception as err:
                logger.error(f"短剧规整：cron 解析失败 {self._cron} - {err}")
        if self._scheduler.get_jobs():
            self._scheduler.start()

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled, "notify": self._notify, "onlyonce": self._onlyonce,
            "cron": self._cron, "strm_root": self._strm_root, "do_poster": self._do_poster,
            "upload_115": self._upload_115, "p115_root": self._p115_root,
        })

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as err:
            logger.error(f"短剧规整：停止服务失败 {err}")

    # ---------- 工具 ----------
    @staticmethod
    def __safe(name: str) -> str:
        s = re.sub(r"[《》]", "", name)
        s = re.sub(r"[~～]", "至", s)
        s = re.sub(r'[<>:"|?*]', "", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s or "未命名"

    @staticmethod
    def __p115():
        """复用 P115StrmHelper 的 115 客户端（读取其 cookies 配置）"""
        cfg = PluginManager().get_plugin_config("P115StrmHelper") or {}
        cookies = cfg.get("cookies")
        if not cookies:
            raise RuntimeError("未取到 P115 凭据（请确认 P115StrmHelper 已配置）")
        from p115client import P115Client
        return P115Client(cookies)

    # ---------- 主逻辑 ----------
    def tidy(self):
        try:
            moved, made, uploaded = 0, 0, 0
            root = Path(self._strm_root)
            if not root.exists():
                logger.warn(f"短剧规整：本地目录不存在 {root}")
                return
            # 1) 扁平 STRM -> 每剧一文件夹
            for f in sorted(root.glob("*.strm")):
                safe = self.__safe(f.stem)
                d = root / safe
                d.mkdir(parents=True, exist_ok=True)
                try:
                    f.rename(d / f"{safe}.strm")
                    moved += 1
                except Exception as err:
                    logger.error(f"短剧规整：移动失败 {f.name} - {err}")
            # 2) 抽帧生成真实剧照
            if self._do_poster:
                for d in sorted(p for p in root.iterdir() if p.is_dir()):
                    poster = d / "poster.jpg"
                    if poster.exists() and poster.stat().st_size > 5000:
                        continue
                    strs = list(d.glob("*.strm"))
                    if not strs:
                        continue
                    try:
                        url = strs[0].read_text(encoding="utf-8", errors="replace").strip()
                        if not url.startswith("http"):
                            continue
                        tmp = d / "poster_tmp.jpg"
                        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "90", "-i", url,
                                        "-frames:v", "1", "-vf", "scale=600:-2", "-q:v", "4", str(tmp)],
                                       capture_output=True, timeout=120)
                        if tmp.exists() and tmp.stat().st_size > 5000:
                            tmp.replace(poster); made += 1
                        elif tmp.exists():
                            tmp.unlink()
                    except Exception as err:
                        logger.error(f"短剧规整：抽帧失败 {d.name} - {err}")
            # 3) 把海报写回 115 对应文件夹（结构保持同名）
            if self._upload_115:
                try:
                    cli = self.__p115()

                    def did(path):
                        g = cli.fs_dir_getid(path)
                        return g.get("id") if isinstance(g, dict) else g

                    for d in sorted(p for p in root.iterdir() if p.is_dir()):
                        poster = d / "poster.jpg"
                        if not (poster.exists() and poster.stat().st_size > 5000):
                            continue
                        pdir = f"{self._p115_root}/{d.name}"
                        try:
                            pid = did(pdir)
                            if not pid:
                                cli.fs_mkdir(d.name, pid=did(self._p115_root))
                            cli.upload(str(poster), pid=did(pdir))
                            uploaded += 1
                            import time
                            time.sleep(1)
                        except Exception as err:
                            logger.error(f"短剧规整：上传 115 失败 {d.name} - {err}")
                except Exception as err:
                    logger.error(f"短剧规整：115 上传阶段异常 - {err}")
            msg = f"短剧规整：规整 {moved}、封面 {made} 张、上传 115 {uploaded} 张"
            logger.info(msg)
            if self._notify and (moved or made or uploaded):
                self.post_message(mtype=NotificationType.Plugin, title="【短剧规整与封面】", text=msg)
        except Exception as err:
            logger.error(f"短剧规整：执行异常 - {err}")

    # ---------- 表单 ----------
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        def row(items):
            return {"component": "VRow", "content": items}

        def col(md, comp, model, label, placeholder=""):
            props = {"model": model, "label": label}
            if placeholder:
                props["placeholder"] = placeholder
            return {"component": "VCol", "props": {"cols": 12, "md": md},
                    "content": [{"component": comp, "props": props}]}

        return [{
            "component": "VForm",
            "content": [
                row([col(4, "VSwitch", "enabled", "启用插件"), col(4, "VSwitch", "notify", "发送通知"),
                     col(4, "VSwitch", "onlyonce", "立即运行一次")]),
                row([col(4, "VTextField", "cron", "执行周期(cron)", "*/30 * * * *"),
                     col(8, "VTextField", "strm_root", "本地 STRM 根目录", "/media/links/115strm/短剧")]),
                row([col(4, "VSwitch", "do_poster", "抽帧生成真实剧照"),
                     col(4, "VSwitch", "upload_115", "封面写回 115"),
                     col(4, "VTextField", "p115_root", "115 目录", "/短剧")]),
            ],
        }], {
            "enabled": False, "notify": True, "onlyonce": False, "cron": "*/30 * * * *",
            "strm_root": "/media/links/115strm/短剧", "do_poster": True,
            "upload_115": True, "p115_root": "/短剧",
        }

    def get_page(self) -> List[dict]:
        return [{"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                 "text": "短剧规整与封面：扁平视频/STRM → 每剧一文件夹；抽帧出真实剧照；可写回 115。"}}]
