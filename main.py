"""
量化交易框架管理系统 - FastAPI主应用

该模块是量化交易框架管理系统的核心FastAPI应用，提供完整的Web API服务。
主要功能包括：

1. 用户认证管理
   - Google Authenticator 2FA登录
   - JWT token管理和自动刷新
   - 用户会话管理

2. 框架管理
   - 基础代码版本获取和下载
   - 框架状态监控和管理
   - PM2进程管理集成

3. 数据中心配置
   - 数据中心参数配置
   - 市值数据下载管理
   - 实时数据路径配置

4. 账户管理
   - 交易账户配置
   - 策略绑定和配置
   - 账户文件生成

5. 文件管理
   - 因子文件上传（时序/截面）
   - 仓管策略上传
   - 文件列表查询

技术特性：
- FastAPI框架，支持自动API文档生成
- 异步处理和后台任务
- 动态CORS配置
- 统一的响应模型
- 完善的错误处理和日志记录
- SQLite数据库持久化

"""

import json
import shutil
import subprocess
import time
from pathlib import Path

from fastapi import (
    FastAPI, HTTPException, Request, Response, BackgroundTasks, UploadFile, File
)
from starlette.middleware.cors import CORSMiddleware

from db.db import init_db
from db.db_ops import (
    get_framework_status, get_all_framework_status, delete_framework_status, get_finished_data_center_status,
    del_user_token, get_user, save_google_secret, update_user_wx_token
)
from model.enum_kit import StatusEnum, UploadFolderEnum
from model.model import (
    LoginRequest, ResponseModel, DataCenterCfgModel, BasicCodeOperateModel, AccountModel, FrameworkCfgModel,
    ApiKeySecretModel
)
from service.basic_code import (
    generate_account_py_file_from_config, extract_variables_from_py,
    generate_account_py_file_from_json
)
from service.command import (
    get_pm2_list, del_pm2, get_pm2_env
)
from service.xbx_api import XbxAPI, TokenExpiredException
from utils.auth import google_login, AuthMiddleware
from utils.constant import DATA_CENTER_ID, PREFIX, CODE_FILE
from utils.log_kit import get_logger

# 初始化日志记录器
logger = get_logger()

# 创建FastAPI应用实例
app = FastAPI(
    title="交易框架管理系统",
    description="提供量化交易框架的完整管理功能，包括用户认证、框架下载、配置管理等",
    version="0.0.1"
)

# 配置认证中间件 - 统一处理JWT token验证和刷新
app.add_middleware(AuthMiddleware)

# 配置CORS中间件 - 允许跨域请求
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],  # 动态配置，允许任意origin
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Refresh-Token", "xbx-Authorization"],  # 暴露token刷新头
)


@app.get(f"/{PREFIX}/declaration")
def declaration(code: str):
    """
    系统声明代码验证接口
    
    验证客户端提供的声明代码是否与系统预设的声明代码一致。
    成功验证后会将代码缓存到data/code.txt文件中，供后续接口使用。
    用于系统身份验证或特定功能的准入控制。
    
    :param code: 客户端提供的声明代码
    :type code: str
    :return: 验证结果
    :rtype: ResponseModel
    
    Returns:
        ResponseModel:
            - data: bool - True表示代码匹配，False表示代码不匹配
    
    Process:
        1. 从code.txt文件读取系统预设的声明代码
        2. 比较客户端代码与系统代码是否一致
        3. 验证成功时缓存代码到data/code.txt文件
        4. 返回验证结果
    
    """
    logger.info(f"收到系统声明代码验证请求，客户端代码: {code}")

    try:
        # 读取系统预设的声明代码
        with open("code.txt", "r", encoding="utf-8") as f:
            local_code = f.read().strip()  # 去除可能的换行符

        logger.debug(f"系统声明代码: {local_code}")

        # 验证代码是否匹配
        is_match = local_code == code

        if is_match:
            # 验证成功，缓存代码到指定文件
            CODE_FILE.write_text(code, encoding="utf-8")
            logger.info(f"系统声明代码验证成功，代码匹配，已缓存到: {CODE_FILE}")
            logger.info(f"缓存文件路径: {CODE_FILE.absolute()}")
        else:
            logger.warning(f"系统声明代码验证失败，代码不匹配")
            logger.debug(f"期望代码内容: {local_code}")
            logger.debug(f"实际代码内容: {code}")

        return ResponseModel.ok(data=is_match)

    except FileNotFoundError:
        logger.error("系统声明代码文件不存在: code.txt")
        logger.error("请确保项目根目录存在code.txt文件")
        return ResponseModel.error(msg="系统配置异常，声明代码文件不存在")
    except Exception as e:
        logger.error(f"系统声明代码验证过程中发生异常: {e}")
        return ResponseModel.error(msg=f"验证过程中发生异常: {str(e)}")


@app.get(f"/{PREFIX}/first")
def first():
    """
    检查系统初始化状态和声明代码状态
    
    检查系统是否为首次使用，并对比系统声明代码与缓存代码的一致性。
    用于前端判断是否需要显示初始化向导和验证用户权限状态。
    
    :return: 包含系统状态信息的响应
    :rtype: ResponseModel
    
    Returns:
        ResponseModel: 
            - data: dict - 包含系统状态信息
                - is_first_use: bool - True表示首次使用，False表示已初始化
                - is_declaration: bool - True表示声明代码已确认，False表示需要验证
    
    Process:
        1. 检查数据库是否有用户记录，判断是否首次使用
        2. 读取系统预设声明代码（code.txt）
        3. 读取缓存的确认声明代码（data/code.txt）
        4. 对比两个代码是否一致
        5. 返回系统状态和声明验证状态

    """
    logger.info("收到系统初始化状态检查请求")

    try:
        # 检查是否首次使用
        is_first_use = get_user() is None
        logger.info(f"首次使用状态检查: {is_first_use} (基于数据库用户记录)")

        # 读取系统预设的声明代码
        with open("code.txt", "r", encoding="utf-8") as f:
            local_code = f.read().strip()  # 去除可能的换行符

        # 读取缓存的确认声明代码
        if CODE_FILE.exists():
            with open(CODE_FILE, "r", encoding="utf-8") as f:
                cache_code = f.read().strip()  # 去除可能的换行符
            logger.debug(f"缓存声明代码读取成功: {cache_code}")
        else:
            # 缓存文件不存在，说明用户从未成功验证过声明代码
            cache_code = ""
            logger.info(f"缓存声明代码文件不存在: {CODE_FILE}，用户尚未验证声明代码")

        # 对比声明代码是否一致
        is_declaration = local_code == cache_code
        logger.info(f"声明代码对比结果: {is_declaration}")

        if is_declaration:
            logger.info("声明代码验证状态: 已确认 ✓")
        else:
            logger.warning("声明代码验证状态: 需要验证 ✗")
            logger.debug(f"系统代码: {local_code}, 缓存代码: {cache_code}")

        result_data = {
            "is_first_use": is_first_use,
            "is_declaration": is_declaration,
        }

        logger.info(f"系统状态检查完成: {result_data}")
        return ResponseModel.ok(data=result_data)

    except Exception as e:
        logger.error(f"系统初始化状态检查失败: {e}")
        return ResponseModel.error(msg=f"系统状态检查失败: {str(e)}")


@app.post(f"/{PREFIX}/login")
def login(body: LoginRequest, response: Response):
    """
    用户登录接口
    
    使用Google Authenticator进行2FA认证登录。
    支持首次登录时绑定Google Secret Key。
    
    :param body: 登录请求数据
    :type body: LoginRequest
    :param response: HTTP响应对象
    :type response: Response
    :return: 登录结果和JWT token
    :rtype: ResponseModel
    
    Process:
        1. 验证Google Authenticator代码
        2. 生成JWT访问token
        3. 保存用户认证信息
        4. 添加wx_token到响应头
        5. 返回token和用户信息
    """
    logger.info(f"用户登录请求，参数: {body}")

    try:
        # 执行Google登录验证
        data = google_login(getattr(body, 'google_secret_key', None), getattr(body, 'code', None))
        logger.info("Google认证验证成功")

        # 保存Google Secret Key到数据库
        success = save_google_secret(body.google_secret_key, data.get('access_token'))
        if not success:
            logger.warning("Google Secret Key已存在，拒绝重复绑定")
            return ResponseModel.error(msg="已经绑定过 secret，请勿重复绑定")

        # 获取用户信息并添加wx_token到响应头
        user = get_user()
        if user and user.wx_token:
            response.headers["xbx-Authorization"] = user.wx_token
            logger.info("wx_token已添加到响应头")
        else:
            logger.info("用户不存在或wx_token为空，跳过响应头设置")

        logger.info("用户登录成功，token已生成")
        return ResponseModel.ok(data=data)

    except Exception as e:
        logger.error(f"用户登录失败: {e}")
        return ResponseModel.error(msg=f"登录失败: {str(e)}")


@app.post(f"/{PREFIX}/logout")
def logout():
    """
    用户登出接口
    
    清除用户的认证token，结束当前会话。
    
    :return: 登出成功响应
    :rtype: ResponseModel
    """
    logger.info("用户登出请求")
    try:
        del_user_token()
        logger.info("用户登出成功，token已清除")
        return ResponseModel.ok(msg="Logged out")
    except Exception as e:
        logger.error(f"用户登出失败: {e}")
        return ResponseModel.error(msg=f"登出失败: {str(e)}")


@app.post(f"/{PREFIX}/user/info")
def user_info(request: Request, background_tasks: BackgroundTasks):
    """
    获取用户信息接口
    
    通过XBX授权token获取用户详细信息，并自动触发数据中心下载。
    
    :param request: HTTP请求对象
    :type request: Request
    :param background_tasks: 后台任务管理器
    :type background_tasks: BackgroundTasks
    :return: 用户信息数据
    :rtype: ResponseModel
    
    Process:
        1. 从请求头获取XBX授权token
        2. 调用XBX API获取用户信息
        3. 设置用户凭据并登录XBX系统
        4. 后台任务下载最新数据中心代码
        5. 返回用户信息
    """
    authorization = request.headers.get("xbx-Authorization", None)
    logger.info(f"获取用户信息请求，token前缀: {authorization[:20] if authorization else 'None'}...")

    if not authorization:
        logger.warning("获取用户信息失败：缺少授权头")
        return ResponseModel.error(code=444, msg="请扫描二维码绑定用户")

    try:
        api = XbxAPI.get_instance()
        data = api.get_user_info(authorization)

        if data:
            logger.info(f"成功获取用户信息，UUID: {data.get('uuid')}")

            # 设置用户凭据并自动登录
            api.set_credentials(data.get("uuid"), data.get("apiKey"))
            if not api.login():
                logger.error("XBX系统登录失败，uuid或apikey错误")
                return ResponseModel.error(code=444, msg="系统认证失败，请重新扫描二维码绑定用户")

            # 更新wx_token到数据库
            update_user_wx_token(authorization)
            logger.info("wx_token已更新到数据库")

            logger.info("XBX系统登录成功，启动数据中心下载任务")
            # 后台任务：下载最新数据中心代码
            background_tasks.add_task(api.download_data_center_latest)

            return ResponseModel.ok(data=data)
        else:
            logger.error("获取用户信息失败：XBX API返回空数据")
            return ResponseModel.error(code=444, msg="获取用户信息失败，请重新扫描二维码绑定用户")

    except TokenExpiredException as e:
        logger.error(f"Token已过期，需要重新认证: {e}")
        return ResponseModel.error(code=444, msg="Token已过期，请重新扫描二维码登录")
    except Exception as e:
        logger.error(f"获取用户信息异常: {e}")
        return ResponseModel.error(code=500, msg=f"获取用户信息异常: {str(e)}")


@app.get(f"/{PREFIX}/basic_code/list")
def get_basic_code():
    """
    获取基础代码版本列表
    
    从XBX服务器获取所有可用的基础代码框架版本信息。
    自动过滤掉数据中心框架，仅返回业务框架。
    同时过滤版本列表，只保留时间大于2025-06-01的版本。
    
    :return: 基础代码版本列表
    :rtype: ResponseModel
    
    Returns:
        ResponseModel:
            - data: list - 框架版本信息列表
            - 每个框架包含：id, name, versions等信息
            - versions中只包含time > "2025-06-01"的版本
    """
    logger.info("获取基础代码版本列表")

    try:
        api = XbxAPI.get_instance()
        result = api.get_basic_code_version()

        if result.get("error") == "token_invalid":
            logger.error("获取基础代码版本失败：XBX token无效")
            raise HTTPException(status_code=401, detail="三方token失效，请重新登录")

        # 过滤掉数据中心框架
        data = result.get('data', [])
        filtered_data = [item for item in data if item.get('id') != DATA_CENTER_ID]

        # 过滤版本列表，只保留time大于2025-06-01的版本（6月份更新的代码，配合当前框架可以使用）
        time_threshold = "2025-06-01"
        for framework in filtered_data:
            versions = framework.get('versions', [])
            # 过滤版本：只保留time大于threshold的版本
            filtered_versions = []
            for version in versions:
                # 这里直接使用字符串比较
                version_time = version.get('time', '')
                if version_time > time_threshold:
                    filtered_versions.append(version)
            framework['versions'] = filtered_versions

        # 统计过滤后的版本数量
        total_versions = sum(len(framework.get('versions', [])) for framework in filtered_data)
        logger.info(
            f"成功获取基础代码版本列表，共{len(filtered_data)}个框架，{total_versions}个版本（时间>{time_threshold}）")
        return ResponseModel.ok(data=filtered_data)

    except TokenExpiredException as e:
        logger.error(f"Token已过期，需要重新认证: {e}")
        return ResponseModel.error(code=444, msg="Token已过期，请重新扫描二维码登录")
    except Exception as e:
        logger.error(f"获取基础代码版本列表异常: {e}")
        return ResponseModel.error(msg=f"获取版本列表失败: {str(e)}")


@app.post(f"/{PREFIX}/save_config/data_center")
def save_config_data_center(data_center_cfg: DataCenterCfgModel):
    """
    保存数据中心配置
    
    保存数据中心的配置参数，包括API配置、数据源配置等。
    如果启用了市值数据，会自动下载历史市值数据。
    
    :param data_center_cfg: 数据中心配置数据
    :type data_center_cfg: DataCenterCfgModel
    :return: 保存结果
    :rtype: ResponseModel
    
    Process:
        1. 验证数据中心下载状态
        2. 下载市值数据（如果启用）
        3. 保存配置到数据库
        4. 生成config.json配置文件
    """
    logger.info(f"保存数据中心配置请求: {data_center_cfg.id}")

    try:
        api = XbxAPI.get_instance()

        # 设置API凭据信息
        data_center_cfg.data_api_key = api.apikey
        data_center_cfg.data_api_uuid = api.uuid
        data_center_cfg.is_first = False

        # 检查数据中心框架状态
        framework_status = get_framework_status(data_center_cfg.id)
        if not framework_status or framework_status.status != StatusEnum.FINISHED or not framework_status.path:
            logger.warning(f"数据中心未下载完成，状态: {framework_status.status if framework_status else 'None'}")
            return ResponseModel.ok(msg='数据中心还没有下载完毕')

        logger.info(f"数据中心框架路径: {framework_status.path}")

        # 下载市值数据（如果启用）
        if data_center_cfg.use_api.coin_cap:
            logger.info("开始下载市值数据...")
            coin_cap_path = (Path(framework_status.path) / 'data' / 'coin_cap')
            if api.download_coin_cap_hist(coin_cap_path):
                logger.info('市值数据下载成功')
            else:
                logger.warning('市值数据下载失败')

        # 生成配置文件
        config_file_path = Path(framework_status.path) / 'config.json'
        config_file_path.write_text(
            json.dumps(data_center_cfg.model_dump(), ensure_ascii=False, indent=2))
        logger.info(f"配置文件已生成: {config_file_path}")

        return ResponseModel.ok()

    except TokenExpiredException as e:
        logger.error(f"Token已过期，需要重新认证: {e}")
        return ResponseModel.error(code=444, msg="Token已过期，请重新扫描二维码登录")
    except Exception as e:
        logger.error(f"保存数据中心配置失败: {e}")
        return ResponseModel.error(msg=f"保存配置失败: {str(e)}")


@app.put(f"/{PREFIX}/save_config/data_center")
def update_config_data_center(data_center_cfg: DataCenterCfgModel):
    """
    更新数据中心配置
    
    更新已存在的数据中心配置参数。
    
    :param data_center_cfg: 数据中心配置数据
    :type data_center_cfg: DataCenterCfgModel
    :return: 更新结果
    :rtype: ResponseModel
    """
    logger.info(f"更新数据中心配置请求: {data_center_cfg.id}")

    try:
        api = XbxAPI.get_instance()

        # 设置API凭据信息
        data_center_cfg.data_api_key = api.apikey
        data_center_cfg.data_api_uuid = api.uuid
        data_center_cfg.is_first = False

        # 更新配置文件
        framework_status = get_framework_status(data_center_cfg.id)
        if framework_status and framework_status.path:
            config_file_path = Path(framework_status.path) / 'config.json'
            config_file_path.write_text(
                json.dumps(data_center_cfg.model_dump(), ensure_ascii=False, indent=2))
            logger.info(f"配置文件已更新: {config_file_path}")

            # 下载市值数据（如果启用）
            if data_center_cfg.use_api.coin_cap:
                logger.info("开始下载市值数据...")
                coin_cap_path = (Path(framework_status.path) / 'data' / 'coin_cap')
                if api.download_coin_cap_hist(coin_cap_path):
                    logger.info('市值数据下载成功')
                else:
                    logger.warning('市值数据下载失败')

        return ResponseModel.ok()

    except TokenExpiredException as e:
        logger.error(f"Token已过期，需要重新认证: {e}")
        return ResponseModel.error(code=444, msg="Token已过期，请重新扫描二维码登录")
    except Exception as e:
        logger.error(f"更新数据中心配置失败: {e}")
        return ResponseModel.error(msg=f"更新配置失败: {str(e)}")


@app.get(f"/{PREFIX}/basic_code/query_config")
def basic_code_query_config(framework_id: str):
    """
    查询框架配置
    
    获取指定框架配置信息。
    
    :param framework_id: 框架ID
    :type framework_id: str
    :return: 配置数据
    :rtype: ResponseModel
    """
    logger.info(f"查询框架配置: {framework_id}")

    try:
        # 验证框架下载状态
        framework_status = get_framework_status(framework_id)
        if not framework_status:
            logger.error(f"框架未下载完成: {framework_id}")
            return ResponseModel.error(msg=f'框架未下载完成')

        config_json_path = Path(framework_status.path) / 'config.json'
        if config_json_path.exists():
            config_json = json.loads(config_json_path.read_text(encoding='utf-8'))
            return ResponseModel.ok(data=config_json)

        return ResponseModel.ok()
    except Exception as e:
        logger.error(f"查询框架配置失败: {e}")
        return ResponseModel.error(msg=f"查询框架配置失败: {str(e)}")


@app.get(f"/{PREFIX}/basic_code/download")
def basic_code_download(framework_id: str, background_tasks: BackgroundTasks):
    """
    启动框架下载任务
    
    异步下载指定的基础代码框架。
    
    :param framework_id: 要下载的框架ID
    :type framework_id: str
    :param background_tasks: 后台任务管理器
    :type background_tasks: BackgroundTasks
    :return: 任务启动结果
    :rtype: ResponseModel
    """
    logger.info(f"启动框架下载任务: {framework_id}")

    try:
        api = XbxAPI.get_instance()
        background_tasks.add_task(api.download_basic_code_for_id, framework_id)
        logger.info(f"框架下载任务已添加到后台队列: {framework_id}")
        return ResponseModel.ok()

    except TokenExpiredException as e:
        logger.error(f"Token已过期，需要重新认证: {e}")
        return ResponseModel.error(code=444, msg="Token已过期，请重新扫描二维码登录")
    except Exception as e:
        logger.error(f"启动框架下载任务失败: {e}")
        return ResponseModel.error(msg=f"启动下载任务失败: {str(e)}")


@app.get(f"/{PREFIX}/basic_code/download/status")
def basic_code_download_status():
    """
    获取框架下载状态
    
    查询所有框架的下载状态信息。
    
    :return: 框架状态列表
    :rtype: ResponseModel
    """
    logger.info("查询框架下载状态")

    try:
        data = get_all_framework_status()
        logger.info(f"成功获取框架状态，共{len(data)}个框架")
        return ResponseModel.ok(data=data)

    except Exception as e:
        logger.error(f"获取框架下载状态失败: {e}")
        return ResponseModel.error(msg=f"获取状态失败: {str(e)}")


@app.delete(f"/{PREFIX}/basic_code")
def basic_code_delete(framework_id: str):
    """
    删除框架
    
    删除指定的框架，包括停止PM2进程、删除文件和数据库记录。
    
    :param framework_id: 要删除的框架ID
    :type framework_id: str
    :return: 删除结果
    :rtype: ResponseModel
    
    Process:
        1. 检查框架状态
        2. 停止PM2进程
        3. 删除数据库记录
        4. 删除本地文件
    """
    logger.info(f"删除框架请求: {framework_id}")

    try:
        framework_status = get_framework_status(framework_id)
        if not framework_status:
            logger.warning(f"框架不存在或未下载完成: {framework_id}")
            return ResponseModel.error(msg=f'框架未下载完成')

        logger.info(f"开始删除框架，路径: {framework_status.path}")

        # 停止并删除PM2进程
        del_pm2(framework_id)
        logger.info(f"PM2进程已停止: {framework_id}")

        # 删除数据库记录
        delete_framework_status(framework_id)
        logger.info(f"数据库记录已删除: {framework_id}")

        # 删除本地文件
        if framework_status.path:
            shutil.rmtree(framework_status.path, ignore_errors=True)
            logger.info(f"本地文件已删除: {framework_status.path}")

        logger.info(f"框架删除完成: {framework_id}")
        return ResponseModel.ok()

    except Exception as e:
        logger.error(f"删除框架失败: {e}")
        return ResponseModel.error(msg=f"删除框架失败: {str(e)}")


# ========== 框架启停/日志 ==========
@app.post(f"/{PREFIX}/basic_code/operate")
def basic_code_operate(operate: BasicCodeOperateModel):
    """
    框架操作接口
    
    对框架进行启动、停止、重启或获取日志等操作。
    支持PM2进程管理集成。
    
    :param operate: 操作请求数据
    :type operate: BasicCodeOperateModel
    :return: 操作结果
    :rtype: ResponseModel
    
    支持的操作类型：
        - start: 启动框架
        - stop: 停止框架
        - restart: 重启框架
        - log: 获取框架日志
    """
    logger.info(f"框架操作请求: {operate.framework_id}, 操作类型: {operate.type}")

    try:
        if operate.type in ["start", "stop", "restart"]:
            logger.info(f"执行PM2操作: {operate.type}")

            framework_status = get_framework_status(operate.framework_id)
            if not framework_status:
                logger.error(f"框架未下载完成: {operate.framework_id}")
                return ResponseModel.error(msg=f'框架未下载完成')

            config_json = Path(framework_status.path) / 'config.json'
            if not config_json.exists():
                return ResponseModel.error(msg=f'当前框架未导入策略配置，禁止实盘启停操作')

            # 检查PM2进程列表
            data = get_pm2_list()
            if not any([item['framework_id'] == operate.framework_id for item in data]):
                logger.info(f"PM2进程不存在，需要先启动: {operate.framework_id}")

                # 启动PM2进程
                startup_config = Path(framework_status.path) / 'startup.json'
                logger.info(f"使用配置文件启动PM2: {startup_config}")

                try:
                    result = subprocess.run(f"pm2 start {startup_config}", env=get_pm2_env(),
                                            shell=True, capture_output=True, text=True)
                    logger.info(f'PM2启动结果: {result.stdout}')
                    if result.stderr:
                        logger.warning(f'PM2启动警告: {result.stderr}')

                    # 启动后直接保存并返回，不需要再执行额外操作
                    subprocess.Popen(f"pm2 save -f", env=get_pm2_env(), shell=True)
                    return ResponseModel.ok(data=f"框架已启动并使用namespace配置")
                except Exception as e:
                    logger.error(f'PM2启动异常: {e}')
                    return ResponseModel.error(msg=f"PM2启动失败: {str(e)}")
            else:
                # 执行对namespace的操作（支持PM2 namespace功能）
                command = f"pm2 {operate.type} {operate.framework_id}"
                logger.info(f"执行PM2命令: {command}")
                subprocess.Popen(command, env=get_pm2_env(), shell=True)
                logger.info(f"PM2操作已执行: {operate.type}")
                subprocess.Popen(f"pm2 save -f", env=get_pm2_env(), shell=True)
                return ResponseModel.ok(data=f"{operate.type} 命令已执行")

        elif operate.type == "log":
            logger.info(f"获取框架日志: {operate.framework_id}, 行数: {operate.lines}")

            try:
                log_command = f"pm2 logs {operate.framework_id} --lines {operate.lines} --nostream"
                result = subprocess.run(log_command, env=get_pm2_env(), shell=True,
                                        capture_output=True, text=True, timeout=30)

                logger.info(f"成功获取框架日志，输出长度: {len(result.stdout)}")
                return ResponseModel.ok(data=result.stdout)

            except subprocess.TimeoutExpired:
                logger.error("获取日志超时")
                return ResponseModel.error(msg="日志获取超时")
            except Exception as e:
                logger.error(f"获取日志异常: {e}")
                return ResponseModel.error(msg=f"日志获取失败: {e}")
        else:
            logger.warning(f"不支持的操作类型: {operate.type}")
            return ResponseModel.error(msg="不支持的操作类型")

    except Exception as e:
        logger.error(f"框架操作失败: {e}")
        return ResponseModel.error(msg=f"命令执行失败: {e}")


# ========== 框架运行状态 ==========
@app.get(f"/{PREFIX}/basic_code/status")
def basic_code_status():
    """
    获取框架运行状态
    
    查询所有框架的PM2进程运行状态。
    
    :return: 框架运行状态列表
    :rtype: ResponseModel
    
    Returns:
        ResponseModel:
            - data: list - PM2进程状态信息列表
            - 包含进程ID、状态、CPU、内存等信息
    """
    logger.info("查询框架运行状态")

    try:
        data = get_pm2_list()
        logger.info(f"成功获取框架运行状态，共{len(data)}个进程")
        return ResponseModel.ok(data=data)
    except Exception as e:
        logger.error(f"获取框架运行状态失败: {e}")
        return ResponseModel.error(msg=f'获取框架运行状态失败, {e}')


@app.get(f"/{PREFIX}/basic_code/cfg/overview")
def basic_code_detail(framework_id: str):
    """
    获取框架配置概览
    
    获取指定框架的详细配置信息。
    
    :param framework_id: 框架ID
    :type framework_id: str
    :return: 框架配置信息
    :rtype: ResponseModel
    
    Note:
        当前为占位实现，后续可扩展具体配置信息
    """
    logger.info(f"获取框架配置概览: {framework_id}")
    # TODO: 实现具体的配置概览逻辑
    return ResponseModel.ok()


# ========== 上传文件(时序因子/截面因子/仓管策略) ==========
@app.post(f"/{PREFIX}/basic_code/upload/file")
def basic_code_upload_file(framework_id: str, upload_folder: UploadFolderEnum, files: list[UploadFile] = File(...)):
    """
    上传文件到框架
    
    上传时序因子、截面因子或仓管策略文件到指定框架。
    
    :param framework_id: 目标框架ID
    :type framework_id: str
    :param upload_folder: 上传文件夹类型
    :type upload_folder: UploadFolderEnum
    :param files: 要上传的文件列表
    :type files: list[UploadFile]
    :return: 上传结果
    :rtype: ResponseModel
    
    支持的文件夹类型：
        - factors: 时序因子
        - sections: 截面因子
        - positions: 仓管策略
    """
    logger.info(f"文件上传请求: 框架={framework_id}, 文件夹={upload_folder.value}, 文件数={len(files)}")

    try:
        framework_status = get_framework_status(framework_id)
        if not framework_status:
            logger.error(f"框架未下载完成: {framework_id}")
            return ResponseModel.error(msg=f'框架未下载完成')

        target_dir = Path(framework_status.path) / upload_folder.value
        logger.info(f"目标上传目录: {target_dir}")

        saved_files = []
        for file in files:
            # 处理子目录情况，提取文件名
            filename = file.filename.split('/')[-1]
            logger.debug(f"处理文件: {file.filename} -> {filename}")

            # 跳过__init__.py文件
            file_path = target_dir / filename
            if file_path.stem == '__init__':
                logger.debug(f"跳过__init__.py文件: {filename}")
                continue

            # 确保目录存在
            file_path.parent.mkdir(parents=True, exist_ok=True)

            # 保存文件
            with open(file_path, "wb") as f:
                content = file.file.read()
                f.write(content)

            logger.info(f"文件保存成功: {file_path}")
            saved_files.append(file_path.stem)

        logger.info(f"文件上传完成，成功保存{len(saved_files)}个文件")
        return ResponseModel.ok(data={"saved_files": saved_files})

    except Exception as e:
        logger.error(f"文件上传失败: {e}")
        return ResponseModel.error(msg=f"文件上传失败: {str(e)}")


# ========== 获取框架文件列表(时序因子/截面因子/仓管策略) ==========
@app.get(f"/{PREFIX}/basic_code/file/list")
def basic_code_file_factor(framework_id: str, upload_folder: UploadFolderEnum):
    """
    获取框架文件列表
    
    获取指定框架中特定文件夹的Python文件列表。
    
    :param framework_id: 框架ID
    :type framework_id: str
    :param upload_folder: 文件夹类型
    :type upload_folder: UploadFolderEnum
    :return: 文件名列表
    :rtype: ResponseModel
    """
    logger.info(f"获取文件列表: 框架={framework_id}, 文件夹={upload_folder.value}")

    try:
        framework_status = get_framework_status(framework_id)
        if not framework_status:
            logger.error(f"框架未下载完成: {framework_id}")
            return ResponseModel.error(msg=f'框架未下载完成')

        target_dir = Path(framework_status.path) / upload_folder.value
        if not target_dir.exists():
            logger.warning(f"目标目录不存在: {target_dir}")
            return ResponseModel.error(msg=f'{target_dir} 路径不存在')

        # 获取Python文件列表（排除__init__.py）
        file_names = [file.name for file in target_dir.iterdir()
                      if file.is_file() and file.suffix == ".py" and file.name != '__init__.py']

        logger.info(f"成功获取文件列表，共{len(file_names)}个文件")
        return ResponseModel.ok(data=file_names)

    except Exception as e:
        logger.error(f"获取文件列表失败: {e}")
        return ResponseModel.error(msg=f"获取文件列表失败: {str(e)}")


@app.post(f"/{PREFIX}/basic_code/global_config")
def basic_code_global_config(framework_cfg: FrameworkCfgModel):
    """
    保存框架全局配置
    
    保存指定框架的全局配置参数，包括数据路径、调试模式、错误通知等。
    自动关联数据中心路径，生成框架运行所需的全局配置文件。
    
    :param framework_cfg: 框架全局配置数据
    :type framework_cfg: FrameworkCfgModel
    :return: 保存结果
    :rtype: ResponseModel
    
    Process:
        1. 验证框架下载状态
        2. 验证数据中心状态
        3. 自动配置实时数据路径
        4. 生成config.json配置文件
        
    Configuration Fields:
        - framework_id: 框架唯一标识
        - realtime_data_path: 实时数据存储路径（自动设置）
        - is_debug: 是否启用调试模式
        - error_webhook_url: 错误通知webhook地址
    """
    logger.info(f"保存框架全局配置: 框架={framework_cfg.framework_id}, 调试模式={framework_cfg.is_debug}")

    try:
        # 验证框架下载状态
        framework_status = get_framework_status(framework_cfg.framework_id)
        if not framework_status:
            logger.error(f"框架未下载完成: {framework_cfg.framework_id}")
            return ResponseModel.error(msg=f'框架未下载完成')

        if not framework_status.path:
            logger.error(f"框架路径为空: {framework_cfg.framework_id}")
            return ResponseModel.error(msg=f"磁盘上未存储框架")

        # 验证数据中心状态
        data_center_status = get_finished_data_center_status()
        if not data_center_status:
            logger.error("数据中心未下载完成")
            return ResponseModel.error(msg="数据中心未下载完成")

        if not data_center_status.path:
            logger.error("数据中心路径为空")
            return ResponseModel.error(msg="数据中心路径异常")

        # 自动配置数据中心存储数据路径
        framework_cfg.realtime_data_path = str(Path(data_center_status.path) / 'data')
        logger.info(f"自动配置实时数据路径: {framework_cfg.realtime_data_path}")

        # 保存JSON配置文件
        config_json_path = Path(framework_status.path) / 'config.json'
        config_data = framework_cfg.model_dump()

        config_json_path.write_text(
            json.dumps(config_data, ensure_ascii=False, indent=2),
            encoding='utf-8'
        )

        logger.info(f"框架全局配置保存成功")
        logger.info(f"配置文件路径: {config_json_path}")
        logger.info(f"配置内容: {config_data}")

        return ResponseModel.ok(msg="全局配置保存成功")

    except Exception as e:
        logger.error(f"保存框架全局配置失败: {e}")
        return ResponseModel.error(msg=f"保存全局配置失败: {str(e)}")


@app.post(f"/{PREFIX}/basic_code/account")
def basic_code_account(account_cfg: AccountModel):
    """
    保存账户配置
    
    保存交易账户的配置信息，包括API密钥、杠杆、黑白名单等。
    同时生成对应的Python配置文件。
    
    :param account_cfg: 账户配置数据
    :type account_cfg: AccountModel
    :return: 保存结果
    :rtype: ResponseModel
    
    Process:
        1. 验证框架状态
        2. 保存JSON配置文件
        3. 生成Python配置文件
    """
    logger.info(f"保存账户配置: 框架={account_cfg.framework_id}, 账户={account_cfg.account_name}")

    try:
        framework_status = get_framework_status(account_cfg.framework_id)
        if not framework_status:
            logger.error(f"框架未下载完成: {account_cfg.framework_id}")
            return ResponseModel.error(msg=f'框架未下载完成')

        if not framework_status.path:
            logger.error(f"框架路径为空: {account_cfg.framework_id}")
            return ResponseModel.error(msg=f"磁盘上未存储框架")

        accounts_dir = Path(framework_status.path) / 'accounts'
        accounts_dir.mkdir(exist_ok=True)
        logger.info(f"账户配置目录: {accounts_dir}")

        # 保存JSON配置文件
        account_json_path = accounts_dir / f'{account_cfg.account_name}.json'
        if account_json_path.exists():
            account_json = json.loads(account_json_path.read_text(encoding='utf-8'))
            if account_json.get('account_config', {}).get('apiKey', ''):
                account_cfg.account_config.apiKey = account_json['account_config']['apiKey']
            if account_json.get('account_config', {}).get('secret', ''):
                account_cfg.account_config.secret = account_json['account_config']['secret']
        account_json_path.write_text(
            json.dumps(account_cfg.model_dump(), ensure_ascii=False, indent=2))
        logger.info(f"JSON配置文件已保存: {account_json_path}")

        # 生成Python配置文件
        generate_account_py_file_from_json(
            account_cfg.account_name,
            account_cfg.model_dump(),
            accounts_dir,
            update_mode=True
        )
        logger.info(f"Python配置文件已生成: {accounts_dir / account_cfg.account_name}.py")

        return ResponseModel.ok()

    except Exception as e:
        logger.error(f"保存账户配置失败: {e}")
        return ResponseModel.error(msg=f"保存账户配置失败: {str(e)}")


@app.get(f"/{PREFIX}/basic_code/account/list")
def basic_code_account_list(framework_id: str):
    """
    获取框架账户列表
    
    获取指定框架的所有账户配置信息。
    
    :param framework_id: 框架ID
    :type framework_id: str
    :return: 账户配置列表
    :rtype: ResponseModel
    """
    logger.info(f"获取账户列表: {framework_id}")

    try:
        framework_status = get_framework_status(framework_id)
        if not framework_status:
            logger.error(f"框架未下载完成: {framework_id}")
            return ResponseModel.error(msg=f'框架未下载完成')

        target_dir = Path(framework_status.path) / 'accounts'
        if not target_dir.exists():
            logger.warning(f"账户目录不存在: {target_dir}")
            return ResponseModel.error(msg=f'{target_dir} 路径不存在')

        file_data = []
        for file in target_dir.iterdir():
            if file.is_file() and file.suffix == ".json" and not file.name.startswith('_'):
                try:
                    account_data = json.loads(file.read_text(encoding='utf-8'))
                    if account_data.get('account_config', {}).get('secret', ''):
                        account_data['account_config']['secret'] = '*' * len(account_data['account_config']['secret'])
                    file_data.append(account_data)
                    logger.debug(f"加载账户配置: {file.name}")
                except Exception as e:
                    logger.warning(f"加载账户配置失败 {file.name}: {e}")

        logger.info(f"成功获取账户列表，共{len(file_data)}个账户")
        return ResponseModel.ok(data=file_data)

    except Exception as e:
        logger.error(f"获取账户列表失败: {e}")
        return ResponseModel.error(msg=f"获取账户列表失败: {str(e)}")


@app.delete(f"/{PREFIX}/basic_code/account")
def basic_code_account_delete(framework_id: str, account_name: str):
    """
    删除框架账户
    
    删除指定的账户配置，包括JSON和Python文件。
    
    :param framework_id: 框架ID
    :type framework_id: str
    :param account_name: 账户名称
    :type account_name: str
    :return: 删除结果
    :rtype: ResponseModel
    """
    logger.info(f"删除账户配置: 框架={framework_id}, 账户={account_name}")

    try:
        framework_status = get_framework_status(framework_id)
        if not framework_status:
            logger.error(f"框架未下载完成: {framework_id}")
            return ResponseModel.error(msg=f'框架未下载完成')

        target_dir = Path(framework_status.path) / 'accounts'
        if not target_dir.exists():
            logger.warning(f"账户目录不存在: {target_dir}")
            return ResponseModel.error(msg=f'{target_dir} 路径不存在')

        # 删除JSON和Python文件
        json_file = target_dir / f'{account_name}.json'
        py_file = target_dir / f'{account_name}.py'

        json_file.unlink(missing_ok=True)
        py_file.unlink(missing_ok=True)

        logger.info(f"账户文件删除完成")
        return ResponseModel.ok()

    except Exception as e:
        logger.error(f"删除账户配置失败: {e}")
        return ResponseModel.error(msg=f"删除账户配置失败: {str(e)}")


# ========== 辅助函数 ==========
def cleanup_expired_temp_files(temp_dir: Path, max_age_hours: int = 24):
    """
    清理过期的临时分段文件
    
    :param temp_dir: 临时文件目录
    :param max_age_hours: 文件最大存活时间（小时）
    """
    if not temp_dir.exists():
        return

    current_time = time.time()
    cutoff_time = current_time - (max_age_hours * 3600)

    try:
        for item in temp_dir.iterdir():
            if item.is_dir():
                # 检查目录的修改时间
                if item.stat().st_mtime < cutoff_time:
                    logger.info(f"清理过期临时目录: {item}")
                    shutil.rmtree(item, ignore_errors=True)
    except Exception as e:
        logger.warning(f"清理临时文件失败: {e}")


@app.post(f"/{PREFIX}/basic_code/account/apikey_secret")
def basic_code_account_apikey_secret(apikey_secret: ApiKeySecretModel):
    """
    接收分段的 apiKey/secret 数据
    
    前端将 apiKey/secret 数据随机拆成 N 分，通过多次请求发送到后端。
    后端根据 keyword 区分 apiKey/secret，通过 sort_id 将 content 拼接起来，
    并将数据保存到 framework_id 对应框架的 path/account 目录下 account_name 的 json 和 py 文件中。
    
    优化后的逻辑：
    - 使用 total 字段来判断数据完整性
    - 相同框架ID、账户名、分段ID的数据会被覆盖
    - 当缓存文件数量等于分段总数时执行合并操作
    - 缓存数据设置过期时间自动清理
    
    :param apikey_secret: 分段数据模型
    :type apikey_secret: ApiKeySecretModel
    :return: 处理结果
    :rtype: ResponseModel
    
    Process:
        1. 验证框架状态和参数
        2. 创建分段缓存文件（支持覆盖）
        3. 检查是否达到完整数量（total）
        4. 如果完整，则按顺序拼接并更新配置
        5. 清理临时缓存文件
    """
    logger.info(f"接收分段数据: 框架={apikey_secret.framework_id}, 账户={apikey_secret.account_name}, "
                f"类型={apikey_secret.keyword}, 分段={apikey_secret.sort_id}/{apikey_secret.total}")

    try:
        # 验证参数有效性
        if apikey_secret.total <= 0:
            logger.error(f"无效的分段总数: {apikey_secret.total}")
            return ResponseModel.error(msg="分段总数必须大于0")

        if apikey_secret.keyword not in ["apiKey", "secret"]:
            logger.error(f"不支持的关键字类型: {apikey_secret.keyword}")
            return ResponseModel.error(msg=f"不支持的关键字类型: {apikey_secret.keyword}")

        # 验证框架下载状态
        framework_status = get_framework_status(apikey_secret.framework_id)
        if not framework_status:
            logger.error(f"框架未下载完成: {apikey_secret.framework_id}")
            return ResponseModel.error(msg=f'框架未下载完成')

        if not framework_status.path:
            logger.error(f"框架路径为空: {apikey_secret.framework_id}")
            return ResponseModel.error(msg=f"磁盘上未存储框架")

        # 验证账户配置文件是否存在
        accounts_dir = Path(framework_status.path) / 'accounts'
        accounts_dir.mkdir(exist_ok=True)

        account_json_path = accounts_dir / f'{apikey_secret.account_name}.json'
        if not account_json_path.exists():
            logger.error(f"账户配置文件不存在: {account_json_path}")
            return ResponseModel.error(msg=f"{apikey_secret.account_name} 账户没有创建，无法配置 {apikey_secret.keyword}")

        # 创建临时缓存目录
        temp_dir = accounts_dir / '.temp'
        temp_dir.mkdir(exist_ok=True)

        # 清理过期的临时文件
        cleanup_expired_temp_files(temp_dir)

        # 创建当前数据的缓存目录（框架ID_账户名_关键字）
        cache_key = f"{apikey_secret.framework_id}_{apikey_secret.account_name}_{apikey_secret.keyword}"
        segment_dir = temp_dir / cache_key
        segment_dir.mkdir(exist_ok=True)

        # 创建元数据文件，记录分段总数和创建时间
        metadata_file = segment_dir / "_metadata.json"
        metadata = {
            "total": apikey_secret.total,
            "keyword": apikey_secret.keyword,
            "framework_id": apikey_secret.framework_id,
            "account_name": apikey_secret.account_name,
            "created_time": time.time(),
            "last_update": time.time()
        }

        # 如果元数据文件存在，检查是否与当前请求匹配
        if metadata_file.exists():
            try:
                existing_metadata = json.loads(metadata_file.read_text(encoding='utf-8'))
                # 如果total不匹配，清理旧缓存重新开始
                if existing_metadata.get("total") != apikey_secret.total:
                    logger.warning(f"分段总数不匹配，清理旧缓存: 旧={existing_metadata.get('total')} vs 新={apikey_secret.total}")
                    # 清理除元数据外的所有分段文件
                    for file in segment_dir.glob("segment_*.txt"):
                        file.unlink()
                    # 更新元数据
                    metadata["created_time"] = time.time()
                else:
                    # 保留创建时间，更新最后修改时间
                    metadata["created_time"] = existing_metadata.get("created_time", time.time())
            except Exception as e:
                logger.warning(f"读取元数据失败，重新创建: {e}")

        # 保存/更新元数据
        metadata_file.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')

        # 将当前分段数据写入缓存文件（覆盖模式）
        segment_file = segment_dir / f"segment_{apikey_secret.sort_id:04d}.txt"
        segment_file.write_text(apikey_secret.content, encoding='utf-8')
        logger.info(f"分段数据已缓存: {segment_file} (覆盖模式)")

        # 统计当前已缓存的分段文件
        segment_files = sorted(segment_dir.glob("segment_*.txt"))
        received_count = len(segment_files)

        logger.info(f"当前已缓存分段: {received_count}/{apikey_secret.total}")

        # 检查是否达到完整数量
        if received_count < apikey_secret.total:
            # 数据不完整，返回部分状态
            segment_numbers = []
            for file in segment_files:
                try:
                    segment_num = int(file.stem.split('_')[1])
                    segment_numbers.append(segment_num)
                except Exception as e:
                    logger.warning(f"解析分段文件名失败 {file}: {e}")

            segment_numbers.sort()
            logger.info(f"数据尚未完整，当前分段: {segment_numbers}")
            return ResponseModel.ok(data={
                "status": "partial",
                "received_segments": received_count,
                "total_segments": apikey_secret.total,
                "segments": segment_numbers,
                "missing_segments": [i for i in range(apikey_secret.total + 1) if i not in segment_numbers],
                "message": f"已接收 {received_count}/{apikey_secret.total} 个分段，等待更多数据"
            })

        # 数据完整，开始拼接和合并操作
        logger.info(f"数据完整，开始拼接操作: {received_count}/{apikey_secret.total}")

        # 按分段ID顺序读取并拼接数据
        complete_data = ""
        successful_segments = []

        for i in range(1, apikey_secret.total + 1):
            segment_file = segment_dir / f"segment_{i:04d}.txt"
            if not segment_file.exists():
                logger.error(f"缺少分段文件: {segment_file}")
                shutil.rmtree(segment_dir, ignore_errors=True)
                return ResponseModel.error(msg=f"缺少分段{i}，数据不完整")

            try:
                content = segment_file.read_text(encoding='utf-8')
                complete_data += content
                successful_segments.append(i)
                logger.debug(f"拼接分段 {i}: 长度={len(content)}")
            except Exception as e:
                logger.error(f"读取分段文件失败 {segment_file}: {e}")
                # 清理临时文件
                shutil.rmtree(segment_dir, ignore_errors=True)
                return ResponseModel.error(msg=f"读取分段{i}失败: {str(e)}")

        # 验证拼接后的数据
        if not complete_data.strip():
            logger.error("拼接后的数据为空")
            # 清理临时文件
            shutil.rmtree(segment_dir, ignore_errors=True)
            return ResponseModel.error(msg="拼接后的数据为空，请检查分段数据")

        logger.info(f"数据拼接完成，总长度: {len(complete_data)}")

        # 读取现有账户配置
        account_data = json.loads(account_json_path.read_text(encoding='utf-8'))
        logger.info("读取现有账户配置")

        # 更新 apiKey 或 secret
        if apikey_secret.keyword == "apiKey":
            account_data["account_config"]["apiKey"] = complete_data
            logger.info(f"更新账户配置中的 apiKey: {complete_data}")
        elif apikey_secret.keyword == "secret":
            account_data["account_config"]["secret"] = complete_data
            logger.info(f"更新账户配置中的 secret: {complete_data}")

        # 保存更新后的 JSON 配置文件
        account_json_path.write_text(
            json.dumps(account_data, ensure_ascii=False, indent=2),
            encoding='utf-8'
        )
        logger.info(f"账户配置 JSON 文件已更新: {account_json_path}")

        # 生成/更新 Python 配置文件
        py_file_path = generate_account_py_file_from_json(
            apikey_secret.account_name,
            account_data,
            accounts_dir,
            update_mode=True  # 保留现有的策略配置
        )
        logger.info(f"账户配置 Python 文件已更新: {py_file_path}")

        # 清理临时缓存文件
        shutil.rmtree(segment_dir, ignore_errors=True)
        logger.info("临时缓存文件已清理")

        # 返回成功结果
        result_data = {
            "status": "complete",
            "keyword": apikey_secret.keyword,
            "data_length": len(complete_data),
            "total_segments": apikey_secret.total,
            "processed_segments": successful_segments,
            "account_name": apikey_secret.account_name,
            "framework_id": apikey_secret.framework_id,
            "message": f"{apikey_secret.keyword} 数据拼接完成并已保存到账户配置"
        }

        logger.info(f"分段数据处理完成: {result_data}")
        return ResponseModel.ok(data=result_data)

    except Exception as e:
        logger.error(f"处理分段数据失败: {e}")
        return ResponseModel.error(msg=f"处理分段数据失败: {str(e)}")


@app.post(f"/{PREFIX}/basic_code/account_binding_strategy")
def basic_code_account_binding_strategy(framework_id: str, account_name: str, file: UploadFile = File(...)):
    """
    账户绑定策略配置
    
    将策略配置文件绑定到指定账户，解析策略参数并生成实盘配置。
    
    :param framework_id: 框架ID
    :type framework_id: str
    :param account_name: 账户名称
    :type account_name: str
    :param file: 策略配置文件
    :type file: UploadFile
    :return: 绑定结果
    :rtype: ResponseModel
    
    Process:
        1. 解析策略配置文件
        2. 提取策略参数
        3. 生成实盘配置文件
        4. 更新账户配置
    """
    logger.info(f"账户绑定策略: 框架={framework_id}, 账户={account_name}, 文件={file.filename}")

    try:
        framework_status = get_framework_status(framework_id)
        if not framework_status:
            logger.error(f"框架未下载完成: {framework_id}")
            return ResponseModel.error(msg=f'框架未下载完成')

        account_path = Path(framework_status.path) / 'accounts' / f'{account_name}.json'
        config_path = Path(framework_status.path) / 'config.json'

        if not account_path.exists():
            logger.error(f"账户配置文件不存在: {account_path}")
            return ResponseModel.error(msg="账户配置文件不存在")

        # 加载账户配置
        account_json = json.loads(account_path.read_text(encoding='utf-8'))
        logger.info("成功加载账户配置")

        # 读取并解析策略文件
        content = file.file.read().decode("utf-8")
        logger.info(f"策略文件内容长度: {len(content)}")

        # 定义需要提取的字段映射
        all_key_map = {
            "strategy_name_from_strategy": "strategy_name",  # 实盘文件中的字段
            "strategy_name_from_backtest": "backtest_name",  # 回测文件中的字段
            "strategy_config": "strategy_config",
            "strategy_pool": "strategy_pool",
            "error_webhook_url": "error_webhook_url",
            "rebalance_mode": "rebalance_mode",
            "simulator_config": "simulator_config"
        }

        extracted, err = extract_variables_from_py(content, all_key_map)
        if err:
            logger.error(f"策略文件解析失败: {err}")
            return ResponseModel.error(msg=err)

        logger.info(f"成功提取策略参数: {list(extracted.keys())}")

        # 确定策略名称（优先级：strategy_name > backtest_name > account_name）
        strategy_name_value = (
                extracted.get("strategy_name_from_strategy") or
                extracted.get("strategy_name_from_backtest") or
                account_name
        )
        logger.info(f"确定策略名称: {strategy_name_value}")

        # 检查数据中心状态
        data_center_status = get_finished_data_center_status()
        if not data_center_status:
            logger.error("数据中心未下载完成")
            return ResponseModel.error(msg="数据中心未下载完成")

        logger.info(f"数据中心路径: {data_center_status.path}")

        # # 生成实盘配置
        # config_json = dict(
        #     realtime_data_path=str(Path(data_center_status.path) / 'data'),
        #     error_webhook_url=extracted.get("error_webhook_url", ''),
        #     is_debug=False,
        #     rebalance_mode=extracted.get("rebalance_mode", None),
        #     simulator_config=extracted.get("simulator_config"),
        # )
        #
        # # 保存实盘配置文件
        # config_path.write_text(json.dumps(config_json, ensure_ascii=False, indent=2))
        # logger.info(f"实盘配置文件已保存: {config_path}")

        # 更新账户配置
        account_json['strategy_name'] = strategy_name_value
        account_json['strategy_config'] = extracted.get("strategy_config")
        account_json['strategy_pool'] = extracted.get("strategy_pool")

        # 生成账户Python文件
        accounts_dir = Path(framework_status.path) / 'accounts'
        generate_account_py_file_from_config(
            account_name,
            account_json,
            extracted,
            strategy_name_value,
            accounts_dir
        )
        logger.info(f"账户Python文件已生成: {accounts_dir / account_name}.py")

        logger.info("账户策略绑定完成")
        return ResponseModel.ok()

    except Exception as e:
        logger.error(f"账户绑定策略失败: {e}")
        return ResponseModel.error(msg=f"绑定策略失败: {str(e)}")


if __name__ == "__main__":
    import uvicorn

    logger.info("初始化数据库...")
    init_db()
    logger.info("数据库初始化完成")

    logger.info(f"启动FastAPI服务器，地址: 0.0.0.0:8000/{PREFIX}")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
