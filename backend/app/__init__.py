"""
MiroFish Backend - Flask应用工厂
"""

import os
import warnings

# 抑制 multiprocessing resource_tracker 的警告（来自第三方库如 transformers）
# 需要在所有其他导入之前设置
warnings.filterwarnings("ignore", message=".*resource_tracker.*")

from flask import Flask, request
from flask_cors import CORS

from .config import Config
from .utils.logger import setup_logger, get_logger


def create_app(config_class=Config):
    """Flask应用工厂函数"""
    app = Flask(__name__)
    app.config.from_object(config_class)
    
    # 设置JSON编码：确保中文直接显示（而不是 \uXXXX 格式）
    # Flask >= 2.3 使用 app.json.ensure_ascii，旧版本使用 JSON_AS_ASCII 配置
    if hasattr(app, 'json') and hasattr(app.json, 'ensure_ascii'):
        app.json.ensure_ascii = False
    
    # 设置日志
    logger = setup_logger('mirofish')
    
    # 只在 reloader 子进程中打印启动信息（避免 debug 模式下打印两次）
    is_reloader_process = os.environ.get('WERKZEUG_RUN_MAIN') == 'true'
    debug_mode = app.config.get('DEBUG', False)
    should_log_startup = not debug_mode or is_reloader_process
    
    if should_log_startup:
        logger.info("=" * 50)
        logger.info("MiroFish Backend 启动中...")
        logger.info("=" * 50)
    
    # 启用CORS
    CORS(app, resources={r"/api/*": {"origins": "*"}})
    
    # Pre-warm graphiti_core's lazy driver imports while we're still on the
    # main thread. Without this, the first time a sim's memory updater
    # thread tries to instantiate a Graphiti client it triggers an import
    # of graphiti_core.driver.neo4j_driver from a worker thread; if any
    # other thread is mid-import inside the same module tree, Python's
    # _ModuleLock detects the circular wait and aborts with
    # "deadlock detected by _ModuleLock". Eager-importing here means the
    # module cache is fully primed by the time any thread asks for it.
    if Config.MEMORY_BACKEND == 'graphiti':
        try:
            import graphiti_core  # noqa: F401
            import graphiti_core.driver  # noqa: F401
            import graphiti_core.driver.neo4j_driver  # noqa: F401
            from graphiti_core import Graphiti  # noqa: F401
            if should_log_startup:
                logger.info("Pre-warmed graphiti_core driver imports")
        except Exception as e:
            logger.warning(f"graphiti_core pre-warm failed (non-fatal): {e}")

    # 注册模拟进程清理函数（确保服务器关闭时终止所有模拟进程）
    from .services.simulation_runner import SimulationRunner
    SimulationRunner.register_cleanup()
    if should_log_startup:
        logger.info("已注册模拟进程清理函数")

    # Zombie state cleanup: previous run may have left simulations stuck
    # at status=preparing/running (debug reload, crash, kill). Without this
    # sweep the UI keeps polling them as if they were live.
    if should_log_startup:
        from .services.simulation_manager import SimulationManager
        try:
            n = SimulationManager.cleanup_zombie_states()
            if n:
                logger.info(f"Zombie state cleanup: {n} simulation(s) auto-recovered to failed")
        except Exception as e:
            logger.warning(f"Zombie state cleanup failed (non-fatal): {e}")
    
    # 请求日志中间件
    @app.before_request
    def log_request():
        logger = get_logger('mirofish.request')
        logger.debug(f"请求: {request.method} {request.path}")
        if request.content_type and 'json' in request.content_type:
            logger.debug(f"请求体: {request.get_json(silent=True)}")
    
    @app.after_request
    def log_response(response):
        logger = get_logger('mirofish.request')
        logger.debug(f"响应: {response.status_code}")
        return response
    
    # 注册蓝图
    from .api import graph_bp, simulation_bp, report_bp
    app.register_blueprint(graph_bp, url_prefix='/api/graph')
    app.register_blueprint(simulation_bp, url_prefix='/api/simulation')
    app.register_blueprint(report_bp, url_prefix='/api/report')
    
    # 健康检查
    @app.route('/health')
    def health():
        return {'status': 'ok', 'service': 'MiroFish Backend'}
    
    if should_log_startup:
        logger.info("MiroFish Backend 启动完成")
    
    return app

