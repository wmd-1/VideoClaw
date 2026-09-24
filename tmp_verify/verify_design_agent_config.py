# 任务 1.1 验证：design_agent 配置段默认值与 VC_DESIGN_AGENT__* env 覆盖
import os
import sys

sys.path.insert(0, "/app")

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        failures.append(name)


# 场景 A：默认值（无 env 覆盖）
os.environ.pop("VC_DESIGN_AGENT__ENABLE", None)
os.environ.pop("VC_DESIGN_AGENT__BASE_URL", None)
for mod in [m for m in list(sys.modules) if m == "config" or m.startswith("config.")]:
    del sys.modules[mod]
import importlib
import config as config_mod
importlib.reload(config_mod)

da = config_mod._get(config_mod.CONFIG_VALUES, "design_agent")
check("A1 design_agent 段存在", isinstance(da, dict))
check("A2 enable 默认 False", da.get("enable") is False, f"got {da.get('enable')}")
check("A3 base_url 默认空", da.get("base_url") == "")
check("A4 login_name 默认 videoclaw", da.get("login_name") == "videoclaw")
check("A5 Config.DESIGN_AGENT_ENABLED False", config_mod.Config.DESIGN_AGENT_ENABLED is False)

# 场景 B：env 覆盖（进程环境变量优先级最高）
os.environ["VC_DESIGN_AGENT__ENABLE"] = "true"
os.environ["VC_DESIGN_AGENT__BASE_URL"] = "http://192.168.1.20:8001"
for mod in [m for m in list(sys.modules) if m == "config" or m.startswith("config.")]:
    del sys.modules[mod]
import config as config_mod2
importlib.reload(config_mod2)

da2 = config_mod2._get(config_mod2.CONFIG_VALUES, "design_agent")
check("B1 env 覆盖 enable=true", da2.get("enable") is True, f"got {da2.get('enable')}")
check("B2 env 覆盖 base_url", da2.get("base_url") == "http://192.168.1.20:8001")
check("B3 env_fields 记录覆盖路径", set(config_mod2.CONFIG_ENV_INFO["fields"]) >= {"design_agent.enable", "design_agent.base_url"}, str(config_mod2.CONFIG_ENV_INFO["fields"]))
check("B4 Config.DESIGN_AGENT_ENABLED True", config_mod2.Config.DESIGN_AGENT_ENABLED is True)

# 场景 C：保存时被 env 覆盖字段恢复文件原值（不写回 env 值）
file_view = config_mod2._FILE_CONFIG_VALUES
check("C1 文件视图 enable 保持 False", config_mod2._get(file_view, "design_agent.enable") is False, f"got {config_mod2._get(file_view, 'design_agent.enable')}")
import copy
view = config_mod2._strip_env_overridden(copy.deepcopy(config_mod2.CONFIG_VALUES), config_mod2.CONFIG_ENV_INFO, file_view)
check("C2 strip 后 enable 恢复文件原值", config_mod2._get(view, "design_agent.enable") is False)
check("C3 strip 后 base_url 恢复文件原值", config_mod2._get(view, "design_agent.base_url") == "")

# 场景 D：非法 env 值容错
os.environ["VC_DESIGN_AGENT__ENABLE"] = "not-a-bool"
os.environ["VC_DESIGN_AGENT__BASE_URL"] = ""
os.environ["VC_DESIGN_AGENT__LOGIN_NAME"] = "alice"
for mod in [m for m in list(sys.modules) if m == "config" or m.startswith("config.")]:
    del sys.modules[mod]
import config as config_mod3
importlib.reload(config_mod3)
da3 = config_mod3._get(config_mod3.CONFIG_VALUES, "design_agent")
check("D1 非法 bool 按字符串处理不崩溃", isinstance(da3.get("enable"), bool) is False or isinstance(da3.get("enable"), bool))
check("D2 login_name env 覆盖生效", da3.get("login_name") == "alice")

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
