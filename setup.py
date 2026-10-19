from plugin import create_plugin_instance

setting = {
    "filepath": __file__,
    "use_db": True,
    "use_default_setting": True,
    "home_module": "list",
    "menu": {
        "uri": __package__,
        "name": "스포츠 중계",
        "list": [
            {"uri": "list", "name": "A 채널"},
            {"uri": "b_test", "name": "B 채널"},
            {"uri": "c_list", "name": "C 채널"},
            {"uri": "setting", "name": "설정"},
            {"uri": "alive", "name": "Alive 연동"},
            {"uri": "log", "name": "로그"},
        ],
    },
    "setting_menu": None,
    "default_route": "single",
}

P = create_plugin_instance(setting)
from .logic import Logic
P.set_module_list([Logic])
# FlaskFarm invokes P.logic.plugin_load(), not PluginModuleBase.plugin_load().
# Bridge the module's periodic worker to the actual framework startup hook.
P.logic.plugin_load = P.module_list[0].plugin_load
