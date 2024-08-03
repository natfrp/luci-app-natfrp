local uci = require "luci.model.uci".cursor()
local sys = require "luci.sys"
local fs = require "nixio.fs"

local m, s, o

local function service_version()
	local file = "/usr/bin/natfrp-service"
	if not fs.stat(file) then
		return '<b style="color: red">文件不存在</b>'
	end

	if not fs.access(file, "x") then
		fs.chmod(file, 755)
	end

	local ver = luci.util.exec("NATFRP_SERVICE_WD=/tmp " .. file .. " -v")
	if not ver or ver == "" then
		return '<b style="color: red">无法获取服务版本, 请检查安装的架构是否正确</b>'
	end
	return '服务版本: ' .. ver
end

m = Map("natfrp", "SakuraFrp 内网穿透")
function m.commit_handler(self, state)
	local current = sys.init.enabled("natfrp")
	local desired = uci:get("natfrp", "main", "enabled") == "1"

	if current == desired then
		return
	end

	if desired then
		sys.init.enable("natfrp")
		sys.init.start("natfrp")
	else
		sys.init.disable("natfrp")
		sys.init.stop("natfrp")
	end
end

s = m:section(NamedSection, "main", "Service Control")

o = s:option(DummyValue, "status")
o.template = "natfrp/service_status"

o = s:option(Flag, "enabled", "启用服务", service_version())

o = s:option(Value, "token", "访问密钥", "留空保留原来的访问密钥")
o.rmempty = true

o = s:option(DummyValue, "_dummy", "远程管理")
o.template = "cbi/nullsection"

o = s:option(Flag, "remote_mgmt", "启用")

o = s:option(Value, "remote_mgmt_pass", "E2E 密码", "留空保留原来的密码, 首次启用必须配置, 最少 8 字符")
o:depends("remote_mgmt", "1")
function o.validate(self, value)
	if value == "" or #value >= 8 then
		return value
	end
	return nil
end
o.rmempty = true

o = s:option(DummyValue, "_dummy", "Web UI")
o.template = "cbi/nullsection"

o = s:option(Flag, "webui", "启用")

o = s:option(Value, "webui_port", "监听端口")
o:depends("webui", "1")
o.default = "4101"
o.rmempty = true
function o.validate(self, value)
	local v = tonumber(value)
	if v > 0 and v <= 65535 then
		return value
	end
	return nil
end

o = s:option(Value, "webui_host", "监听地址", "需使用 HTTPS 进行访问, 默认监听所有接口")
o:depends("webui", "1")
o.default = "0.0.0.0"
o.rmempty = true

o = s:option(Value, "webui_pass", "连接密码", "留空保留原来的密码, 最少 8 字符")
o:depends("webui", "1")
o.rmempty = true
function o.validate(self, value)
	if value == "" or #value >= 8 then
		return value
	end
	return nil
end

o = s:option(Flag, "check_update", "检查更新", "下载更新会占用路由器存储空间, 请谨慎启用")
o:depends("webui", "1")
o.rmempty = true

return m
