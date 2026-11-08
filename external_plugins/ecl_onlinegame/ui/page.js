const { t, renderSkinAvatar, ...components } = Controls;

const ConnectorPlayerAvatar = {
    components,
    props: {
        skinBase64: String,
        name: String,
        size: { type: Number, default: 40 },
    },
    template: '<UiAvatar :src="headSrc" :name="name" :size="size" />',
    data() {
        return { headSrc: "", request: 0 };
    },
    mounted() {
        this.updateHead();
    },
    beforeUnmount() {
        this.request++;
    },
    watch: { skinBase64: "updateHead", size: "updateHead" },
    methods: {
        async updateHead() {
            const request = ++this.request;
            if (!this.skinBase64) {
                this.headSrc = "";
                return;
            }
            const result = await renderSkinAvatar(
                "data:image/png;base64," + this.skinBase64,
                this.size,
            );
            if (request === this.request) this.headSrc = result || "";
        },
    },
};

const PlayerList = {
    components: { ...components, ConnectorPlayerAvatar },
    props: {
        players: { type: Array, default: () => [] },
        hostControls: Boolean,
        busy: Boolean,
    },
    emits: ["kick"],
    template: playerTemplate,
    methods: { t },
};

const NatDetectModal = {
    components,
    props: { visible: Boolean, busy: Boolean, result: Object, error: String },
    emits: ["update:visible", "detect"],
    template: natTemplate,
    computed: {
        isDetecting() {
            return this.busy && !this.result;
        },
        state() {
            return this.error ? "error" : this.result ? "ready" : "idle";
        },
        failureText() {
            return this.error || "";
        },
        detectedLabel() {
            return "";
        },
        resultLabel() {
            const kind =
                this.result && (this.result.detailType || this.result.type);
            return t(
                !kind || kind === "unknown"
                    ? "connect.nat.unknownType"
                    : `connect.nat.${kind}`,
            );
        },
        resultTone() {
            return (
                { cone: "success", symmetric: "warning", blocked: "error" }[
                    this.result && this.result.type
                ] || "default"
            );
        },
        publicAddress() {
            if (!this.result || !this.result.publicIp) return "";
            const host = this.result.publicIp.includes(":")
                ? `[${this.result.publicIp}]`
                : this.result.publicIp;
            if (!this.result.publicPort) return host;
            const port =
                this.result.publicPortEnd &&
                this.result.publicPortEnd !== this.result.publicPort
                    ? `${this.result.publicPort}-${this.result.publicPortEnd}`
                    : this.result.publicPort;
            return `${host}:${port}`;
        },
    },
    methods: { t },
};

const NodeSettingsModal = {
    components,
    props: { visible: Boolean, busy: Boolean, mode: String, text: String },
    emits: ["update:visible", "save"],
    data() {
        return { draftMode: this.mode, draftText: this.text };
    },
    watch: {
        visible(value) {
            if (value) {
                this.draftMode = this.mode;
                this.draftText = this.text;
            }
        },
    },
    computed: {
        modes() {
            return ["automatic", "append", "custom"].map((value) => ({
                value,
                label: t(`advanced.nodesMode.${value}`),
            }));
        },
    },
    methods: { t },
    template: `<Modal :visible="visible" :title="t('advanced.nodesEntry')" width="560px" maskClosable @update:visible="$emit('update:visible', $event)">
    <div data-plugin="ECL-OnlineGame" class="node-settings-form">
      <SettingRow :label="t('advanced.nodesPolicy')">
        <NSelect v-model:value="draftMode" class="node-settings-select" :options="modes" :disabled="busy" :inputProps="{ 'aria-label': t('advanced.nodesPolicy') }" />
      </SettingRow>
      <div v-if="draftMode !== 'automatic'" class="node-settings-addresses">
        <label for="onlinegame-node-addresses">{{ t('advanced.nodesAddresses') }}</label>
        <NInput v-model:value="draftText" type="textarea" :autosize="{ minRows: 3, maxRows: 8 }" placeholder="tcp://relay.example.com:11010" :disabled="busy" :inputProps="{ id: 'onlinegame-node-addresses', 'aria-label': t('advanced.nodesAddresses') }" />
      </div>
    </div>
    <template #footer>
      <NButton @click="$emit('update:visible', false)">{{ t('common.cancel') }}</NButton>
      <NButton type="primary" :disabled="busy" @click="$emit('save', { mode: draftMode, text: draftText })">{{ t('common.save') }}</NButton>
    </template>
  </Modal>`,
};

module.exports = {
    name: "OnlineGamePage",
    components: {
        ...components,
        ConnectorPlayerAvatar,
        PlayerList,
        NatDetectModal,
        NodeSettingsModal,
    },
    data() {
        return {
            status: { mode: "idle", players: [] },
            error: "",
            loading: false,
            busy: false,
            stopped: false,
            generation: 0,
            icons: {},
            operationLabel: "",
            roomCode: "",
            port: 25565,
            instanceRef: null,
            selectedInstanceKey: "",
            instanceOptions: [],
            portOptions: [],
            hostStep: 1,
            detectedPort: null,
            scanning: false,
            scanPhase: "",
            natVisible: false,
            nodesVisible: false,
            nodeMode: "automatic",
            nodeText: "",
            nat: null,
            natError: "",
            nodeOptions: [
                { label: "公共节点", value: "automatic" },
                { label: "公共与自定义节点", value: "append" },
                { label: "仅自定义节点", value: "custom" },
            ],
        };
    },
    computed: {
        serviceReady() {
            return !this.stopped;
        },
        createRoomDisabled() {
            return !this.serviceReady || this.busy;
        },
        runningInstanceOptions() {
            return this.instanceOptions;
        },
        runningInstances() {
            return this.instanceOptions.map((item) => ({
                id: item.value,
                name: item.label,
            }));
        },
        selectedInstance() {
            return (
                this.runningInstances.find(
                    (item) => item.id === this.selectedInstanceKey,
                ) || null
            );
        },
        hostPlayer() {
            const players = (this.status && this.status.players) || [];
            const player = players.find((player) => player.kind === "host");
            return player
                ? {
                      ...player,
                      iconBase64: this.icons[player.machineId]?.base64 || "",
                  }
                : null;
        },
        hostName() {
            return this.hostPlayer ? this.hostPlayer.name || "" : "";
        },
        serverAddress() {
            if (!this.status) return "";
            return `${this.status.mcHost || ""}:${this.status.mcPort || ""}`;
        },
        roomPlayerCount() {
            return (this.status && this.status.players) || [];
        },
    },
    watch: {
        natVisible(value) {
            if (value && !this.nat) void this.detectNat();
        },
        "status.mode"(value) {
            if (value === "idle") {
                this.hostStep = 1;
                void this.loadInstances();
            }
        },
    },
    mounted() {
        this.refresh();
        this.loadInstances();
        this.pollTimer = setInterval(() => this.refresh(), 2500);
    },
    beforeUnmount() {
        this.stopped = true;
        this.generation++;
        clearInterval(this.pollTimer);
        clearTimeout(this.operationTimer);
    },
    methods: {
        t(key, values) {
            return t(key, values || {});
        },
        async call(name, payload) {
            const method = [
                "status",
                "operation",
                "nodes_get",
                "instances",
                "player_icon",
            ].includes(name)
                ? "queryPluginCommand"
                : "callPluginCommand";
            const response = await window.__plugin_sdk__.api[method](
                `ECL-OnlineGame:${name}`,
                payload === undefined ? {} : { payload },
            );
            if (!response || !response.success)
                throw new Error("插件操作未完成，请检查权限和运行状态");
            return response.data.result;
        },
        async refresh() {
            if (this.loading || this.stopped) return;
            this.loading = true;
            const generation = this.generation;
            try {
                const status = await this.call("status");
                let offset = status.next_offset;
                while (offset !== null) {
                    const page = await this.call("status", { offset });
                    if (
                        page.roomCode !== status.roomCode ||
                        page.mode !== status.mode
                    )
                        break;
                    status.players.push(...page.players);
                    offset = page.next_offset;
                }
                if (!this.stopped && generation === this.generation) {
                    status.players = status.players.map((player) => ({
                        ...player,
                        iconBase64: this.icons[player.machineId]?.base64 || "",
                    }));
                    this.status = status;
                    void this.loadIcons(status.players, generation);
                }
            } catch (error) {
                if (!this.stopped) this.error = error.message;
            } finally {
                if (!this.stopped) this.loading = false;
            }
        },
        async run(name, payload, label, done) {
            if (this.busy || this.stopped) return;
            this.busy = true;
            this.error = "";
            this.operationLabel = label;
            const generation = ++this.generation;
            try {
                const receipt = await this.call(name, payload);
                const check = async () => {
                    if (this.stopped || generation !== this.generation) return;
                    try {
                        const operation = await this.call("operation", {
                            operation_ref: receipt.operation_ref,
                        });
                        if (this.stopped || generation !== this.generation)
                            return;
                        if (operation.state === "running") {
                            this.operationTimer = setTimeout(check, 500);
                            return;
                        }
                        this.busy = false;
                        if (operation.state === "failed") {
                            this.error = operation.error;
                            this.scanning = false;
                            if (name === "nat_type") this.natError = operation.error;
                        } else if (done) done(operation.value);
                        await this.refresh();
                    } catch (error) {
                        if (!this.stopped) {
                            this.busy = false;
                            this.scanning = false;
                            this.error = error.message;
                            if (name === "nat_type") this.natError = error.message;
                        }
                    }
                };
                await check();
            } catch (error) {
                if (!this.stopped) {
                    this.busy = false;
                    this.scanning = false;
                    this.error = error.message;
                    if (name === "nat_type") this.natError = error.message;
                }
            }
        },
        async loadIcons(players, generation) {
            for (const player of players) {
                if (this.stopped || generation !== this.generation) return;
                if (
                    !player.iconRevision ||
                    (this.icons[player.machineId] &&
                        this.icons[player.machineId].revision ===
                            player.iconRevision)
                )
                    continue;
                try {
                    let offset = 0,
                        encoded = "",
                        bytes = 0;
                    while (offset !== null) {
                        const part = await this.call("player_icon", {
                            machine_id: player.machineId,
                            revision: player.iconRevision,
                            offset,
                        });
                        const block = atob(part.content);
                        bytes += block.length;
                        if (bytes > 512 * 1024)
                            throw new Error("头像超过大小限制");
                        encoded += block;
                        offset = part.next_offset;
                    }
                    if (!this.stopped && generation === this.generation) {
                        this.icons[player.machineId] = {
                            revision: player.iconRevision,
                            base64: btoa(encoded),
                            src: "data:image/png;base64," + btoa(encoded),
                        };
                        player.iconBase64 = this.icons[player.machineId].base64;
                    }
                } catch {
                    /* 头像刷新失败不影响房间状态，下一次状态刷新重试。 */
                }
            }
        },
        async loadInstances() {
            try {
                const page = await this.call("instances");
                this.instanceOptions = page.items
                    .filter((item) => item.running_count > 0)
                    .map((item) => ({
                        label:
                            item.alias || item.version_id || item.instance_ref,
                        value: item.instance_ref,
                    }));
            } catch (error) {
                this.error =
                    "请先在插件权限中批准查看实例，并选择允许访问的游戏目录";
            }
        },
        goToPortStep() {
            if (!this.selectedInstanceKey) return;
            this.instanceRef = this.selectedInstanceKey;
            this.hostStep = 2;
            this.$nextTick(() => this.startPortScan());
        },
        goToManualPort() {
            this.hostStep = 2;
            this.$nextTick(() => this.startPortScan());
        },
        goBackToInstanceStep() {
            this.hostStep = 1;
        },
        quickCreate(instanceRef) {
            this.selectedInstanceKey = instanceRef.id;
            return this.goToPortStep();
        },
        hostInstance() {
            return this.run(
                "host_instance",
                { instance_ref: this.instanceRef },
                "正在检测实例并建房",
            );
        },
        hostPort() {
            return this.run(
                "host_port",
                { port: Number(this.port) },
                "正在创建房间",
            );
        },
        createManualPort() {
            return this.hostPort();
        },
        createDetectedPort() {
            if (this.detectedPort) this.port = this.detectedPort;
            return this.hostPort();
        },
        async startPortScan() {
            if (this.busy || this.stopped) return;
            this.scanning = true;
            this.detectedPort = null;
            this.scanPhase = "detecting";
            return this.run(
                    "detect_ports",
                    undefined,
                    "正在检测端口",
                    (value) => {
                        const ports = (value && value.ports) || [];
                        this.portOptions = ports.map((port) => ({
                            label: String(port),
                            value: port,
                        }));
                        if (!ports.length) { this.scanning = false; return; }
                        this.scanPhase = "searching";
                        void this.run("search_mc_port", { ports }, "正在确认 Minecraft 端口", result => {
                            this.detectedPort = result.port || null;
                            this.scanning = false;
                        });
                    },
                );
        },
        detectPorts() {
            return this.startPortScan();
        },
        joinRoom() {
            return this.run(
                "join",
                { code: this.roomCode.trim() },
                "正在加入房间",
            );
        },
        leave() {
            return this.run("leave", undefined, "正在退出房间");
        },
        kick(machineId) {
            return this.run("kick", { machine_id: machineId }, "正在移出玩家");
        },
        refreshStatus() {
            return this.refresh();
        },
        async detectNat() {
            this.natVisible = true;
            this.nat = null;
            this.natError = "";
            await this.run("nat_type", undefined, "正在检测 NAT", (result) => {
                this.nat = result;
            });
        },
        async toggleNodes() {
            if (this.nodesVisible) {
                this.nodesVisible = false;
                return;
            }
            try {
                const result = await this.call("nodes_get");
                this.nodeMode = result.mode;
                this.nodeText = (result.nodes || []).join("\n");
                this.nodesVisible = true;
            } catch (error) {
                this.error = error.message;
            }
        },
        openConnectorSettings() {
            return this.toggleNodes();
        },
        async saveNodes(values) {
            if (this.busy) return;
            if (values) {
                this.nodeMode = values.mode;
                this.nodeText = values.text;
            }
            this.busy = true;
            this.error = "";
            try {
                await this.call("nodes_set", {
                    mode: this.nodeMode,
                    nodes: this.nodeText.split(/\r?\n/),
                });
                this.nodesVisible = false;
            } catch (error) {
                this.error = error.message;
            } finally {
                this.busy = false;
            }
        },
        clearRoomCode() {
            this.roomCode = "";
        },
        async pasteRoomCode() {
            try {
                this.roomCode = await navigator.clipboard.readText();
            } catch {
                this.error = "无法读取剪贴板，请手动粘贴房间码";
            }
        },
        async copyCurrentRoomCode() {
            try {
                await navigator.clipboard.writeText(this.status.roomCode);
            } catch {
                this.error = "无法写入剪贴板，请手动复制房间码";
            }
        },
        async copyServerAddress() {
            try {
                await navigator.clipboard.writeText(this.serverAddress);
            } catch {
                this.error = "无法写入剪贴板，请手动复制连接地址";
            }
        },
    },
};
