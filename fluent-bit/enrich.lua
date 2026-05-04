-- 從 docker json-file driver 的 log 路徑萃出 container id,
-- 再讀同目錄的 config.v2.json 拿 container name,
-- 讓 VictoriaLogs 能用 container_name 做 stream field 過濾。

local function read_container_name(cid)
    local f = io.open("/var/lib/docker/containers/" .. cid .. "/config.v2.json", "r")
    if not f then return nil end
    local content = f:read("*a")
    f:close()
    -- config.v2.json 的 "Name" 欄位形如 "/autotest-backend";去掉前綴斜線
    local name = string.match(content, '"Name":"/?([^"]+)"')
    return name
end

function add_container_meta(tag, ts, record)
    local path = record["file_path"]
    if not path then return 0, ts, record end

    local cid = string.match(path, "/containers/([^/]+)/")
    if not cid then return 0, ts, record end

    record["container_id"] = string.sub(cid, 1, 12)
    local name = read_container_name(cid)
    if name then
        record["container_name"] = name
    end
    return 2, ts, record
end
