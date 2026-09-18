# 野生菌出口时效链

连接山区采集、菌种鉴别、加工批次、检疫证书、口岸预约和冷链运输。项目以领域契约约定参与者、状态和不可破坏的业务原则，基础服务提供稳定的运行检查与契约读取接口，便于各模块围绕同一语义协作。

运行 `python3 service.py --check` 可核对服务配置；执行 `python3 service.py --port 8000` 后，可访问 `/health` 与 `/contract`。使用 `python3 -m unittest -v` 运行基础契约测试。

