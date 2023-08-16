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

	local data = luci.jsonc.parse(luci.util.exec("/etc/init.d/natfrp info"))
	if type(data) == "table" and type(data.natfrp) == "table" and type(data.natfrp.instances) == "table" and type(data.natfrp.instances.main) == "table" then
		result.running = data.natfrp.instances.main.running
		result.pid = data.natfrp.instances.main.pid
	end

	http.prepare_content("application/json")
	http.write_json(result)
end
