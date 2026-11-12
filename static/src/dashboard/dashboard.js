/** Tableau de bord de Comprendre : usage et coût de l'IA, codes IA, ventes. */
import { loadBundle } from "@web/core/assets";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, onWillStart, useEffect, useRef, useState } from "@odoo/owl";

// Couleurs des séries (palette catégorielle validée, ordre fixe) : bleu = quota gratuit, orange = codes IA.
const FREE = "#2a78d6";
const CODED = "#eb6834";
const COST = "#1baf7a";
const INK = "#5f6b6e";
const GRID = "rgba(0,0,0,0.06)";

const nf = (digits = 0) => new Intl.NumberFormat("fr-FR", { minimumFractionDigits: digits, maximumFractionDigits: digits });

export class ComprendreDashboard extends Component {
    static template = "comprendre_backend.Dashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({ days: 30, data: null, loading: true, error: null, table: false });
        this.usageRef = useRef("usageChart");
        this.costRef = useRef("costChart");
        this.charts = [];
        onWillStart(async () => {
            await loadBundle("web.chartjs_lib");
            await this.load();
        });
        // Redessine après chaque rendu où les données ou la vue (graphique / tableau) ont changé.
        useEffect(
            () => {
                this.renderCharts();
                return () => this.destroyCharts();
            },
            () => [this.state.data, this.state.table]
        );
    }

    async load() {
        this.state.loading = true;
        try {
            this.state.data = await this.orm.call("comprendre.dashboard", "get_data", [this.state.days]);
            this.state.error = null;
        } catch (e) {
            this.state.error = (e && e.data && e.data.message) || String(e);
        } finally {
            this.state.loading = false;
        }
    }

    async setDays(days) {
        if (days === this.state.days && this.state.data) return;
        this.state.days = days;
        await this.load();
    }

    async refresh() {
        await this.load();
    }

    toggleTable() {
        this.state.table = !this.state.table;
    }

    // -- Formats ----------------------------------------------------------
    int(v) { return nf(0).format(v || 0); }
    nf1(v) { return nf(1).format(v || 0); }
    usd(v) { return "$ " + nf(v >= 100 ? 0 : 2).format(v || 0); }
    usdFine(v) { return "$ " + nf(3).format(v || 0); }
    local(v) {
        const s = this.state.data && this.state.data.sales;
        return nf(0).format(Math.round(v || 0)) + " " + (s && s.currency === "XOF" ? "F CFA" : (s ? s.symbol : ""));
    }
    pct(v) { return nf(v < 10 && v > 0 ? 1 : 0).format(v || 0) + " %"; }
    date(v) {
        if (!v) return "—";
        const [y, m, d] = v.split("-");
        return `${d}/${m}/${y}`;
    }
    shortDay(v) {
        const [, m, d] = v.split("-");
        return `${d}/${m}`;
    }
    daysLeft(n) {
        if (n === null || n === undefined) return "";
        if (n === 0) return "aujourd'hui";
        if (n === 1) return "demain";
        return `dans ${n} j`;
    }
    get freeShare() {
        const u = this.state.data.usage;
        return u.done ? (100 * u.free) / u.done : 0;
    }
    get margin() {
        const s = this.state.data.sales;
        return s.revenue - s.cost_sold_local;
    }

    // -- Navigation : du chiffre aux lignes ----------------------------------
    open(model, name, domain = [], context = {}, views = ["list", "form"]) {
        this.action.doAction({
            type: "ir.actions.act_window",
            name,
            res_model: model,
            domain,
            context,
            views: views.map((v) => [false, v]),
            target: "current",
        });
    }
    period() {
        return [["day", ">=", this.state.data.start], ["day", "<=", this.state.data.today]];
    }
    openRequests(extra = [], name = "Demandes de quiz IA") {
        this.open("comprendre.ai.request", name, [...this.period(), ...extra], {}, ["list", "pivot", "graph", "form"]);
    }
    openCodes(key, name) {
        this.open("comprendre.ai.code", name, this.state.data.codes.domains[key] || [], { active_test: false });
    }
    openCode(id) {
        this.action.doAction({ type: "ir.actions.act_window", res_model: "comprendre.ai.code", res_id: id,
            views: [[false, "form"]], target: "current" });
    }
    openBatch(id) {
        this.action.doAction({ type: "ir.actions.act_window", res_model: "comprendre.ai.code.batch", res_id: id,
            views: [[false, "form"]], target: "current" });
    }
    openAlert(alert) {
        if (alert.action === "to_validate") this.action.doAction("comprendre_backend.action_orders_to_validate");
        else if (alert.action === "settings") this.openSettings();
        else if (alert.action === "failures") {
            this.open("comprendre.ai.request", "Échecs du jour",
                [["day", "=", this.state.data.today], ["state", "in", ["failed", "rejected"]]]);
        }
    }
    createCodes() { this.action.doAction("comprendre_backend.action_code_generate"); }
    openSettings() { this.action.doAction("comprendre_backend.action_settings"); }

    // -- Graphiques (un axe chacun : quiz par jour, coût par jour) ------------
    destroyCharts() {
        for (const c of this.charts) c.destroy();
        this.charts = [];
    }

    renderCharts() {
        this.destroyCharts();
        const data = this.state.data;
        if (!data || this.state.table || !window.Chart) return;
        const labels = data.series.map((p) => this.shortDay(p.day));
        const common = {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: { position: "top", align: "start", labels: { color: INK, boxWidth: 12, boxHeight: 12, usePointStyle: false } },
                tooltip: { backgroundColor: "#fff", titleColor: "#0e3a44", bodyColor: "#0e3a44", borderColor: "#d0d7d9",
                    borderWidth: 1, padding: 10 },
            },
            scales: {
                x: { stacked: true, grid: { display: false }, ticks: { color: INK, maxRotation: 0, autoSkip: true, maxTicksLimit: 10 } },
                y: { stacked: true, beginAtZero: true, grid: { color: GRID }, border: { display: false },
                    ticks: { color: INK, precision: 0 } },
            },
        };
        const bar = { borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: "bottom", borderColor: "#fff",
            borderWidth: { top: 2 }, maxBarThickness: 28 };
        if (this.usageRef.el) {
            this.charts.push(new window.Chart(this.usageRef.el, {
                type: "bar",
                data: { labels, datasets: [
                    { label: "Quota gratuit", data: data.series.map((p) => p.free), backgroundColor: FREE, ...bar,
                        borderRadius: 0 },
                    { label: "Codes IA", data: data.series.map((p) => p.coded), backgroundColor: CODED, ...bar },
                ] },
                options: common,
            }));
        }
        if (this.costRef.el) {
            const options = JSON.parse(JSON.stringify(common));
            options.plugins.legend.display = false;
            options.scales.y.ticks = { color: INK, callback: (v) => "$" + v };
            options.plugins.tooltip.callbacks = { label: (ctx) => ` Coût : ${this.usdFine(ctx.parsed.y)}` };
            options.plugins.tooltip.backgroundColor = "#fff";
            this.charts.push(new window.Chart(this.costRef.el, {
                type: "bar",
                data: { labels, datasets: [
                    { label: "Coût estimé ($)", data: data.series.map((p) => p.cost), backgroundColor: COST, ...bar },
                ] },
                options,
            }));
        }
    }
}

registry.category("actions").add("comprendre_dashboard", ComprendreDashboard);
