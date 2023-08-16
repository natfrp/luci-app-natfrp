local http = require "luci.http"

module("luci.controller.natfrp", package.seeall)

function index()
	if not nixio.fs.access("/etc/config/natfrp") then
		return
	end

	entry({"admin", "services", "natfrp"}, cbi("natfrp"), "Sakura Frp", 10).dependent=false

	entry({"admin", "services", "natfrp", "status"}, call("action_status"))
end

function action_status()
	local result = {
		running = false,
	}

	local pid = luci.sys.exec("pidof natfrp-service")
	if pid ~= "" then
		result.running = true
		result.pid = pid
	end

	http.prepare_content("application/json")
	http.write_json(result)
end
