import { StrictMode, useEffect, useRef, useState, type ReactNode } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useDialog } from "@/components/common/useDialog";

/**
 * Arnés de prueba que reproduce cómo lo usan los consumidores reales
 * (AuthModal, FeedbackModal, MessageRatingPopover, ConversationSearchDialog,
 * el modal administrativo de usuarios y `MobileSidebar`): un disparador
 * externo que abre el panel, y el panel mismo montado
 * condicionalmente sobre `open` (nunca oculto con CSS), tal como exige el
 * hook para que `panelRef.current` exista al momento de enfocar.
 */
function Harness({
	closeBlocked = false,
	useInitialFocusRef = false,
}: {
	closeBlocked?: boolean;
	useInitialFocusRef?: boolean;
}) {
	const [open, setOpen] = useState(false);
	const specialRef = useRef<HTMLButtonElement>(null);
	const { panelRef, titleId } = useDialog({
		open,
		onClose: () => setOpen(false),
		closeBlocked,
		initialFocusRef: useInitialFocusRef ? specialRef : undefined,
	});

	return (
		<div>
			<button type="button" onClick={() => setOpen(true)}>
				Abrir
			</button>
			{open && (
				<div ref={panelRef} tabIndex={-1} role="dialog" aria-labelledby={titleId}>
					<h2 id={titleId}>Diálogo de prueba</h2>
					<button type="button">Primero</button>
					{useInitialFocusRef && (
						<button type="button" ref={specialRef}>
							Especial
						</button>
					)}
					<button type="button">Segundo</button>
					<button type="button">Último</button>
				</div>
			)}
		</div>
	);
}

/** Panel sin ningún control habilitado (todo deshabilitado, como durante
 *  un envío en curso). */
function NoFocusableHarness() {
	const [open, setOpen] = useState(false);
	const { panelRef, titleId } = useDialog({ open, onClose: () => setOpen(false) });

	return (
		<div>
			<button type="button" onClick={() => setOpen(true)}>
				Abrir
			</button>
			{open && (
				<div ref={panelRef} tabIndex={-1} role="dialog" aria-labelledby={titleId}>
					<h2 id={titleId}>Diálogo sin controles habilitados</h2>
					<button type="button" disabled>
						Deshabilitado
					</button>
				</div>
			)}
		</div>
	);
}

/** El control enfocado desaparece al pasar de una rama de contenido a
 *  otra dentro del mismo diálogo (formulario → éxito), tal como ocurre
 *  en FeedbackModal/MessageRatingPopover. */
function TransitionHarness() {
	const [open, setOpen] = useState(false);
	const [phase, setPhase] = useState<"form" | "success">("form");
	const { panelRef, titleId } = useDialog({ open, onClose: () => setOpen(false) });

	return (
		<div>
			<button
				type="button"
				onClick={() => {
					setPhase("form");
					setOpen(true);
				}}
			>
				Abrir
			</button>
			{open && (
				<div ref={panelRef} tabIndex={-1} role="dialog" aria-labelledby={titleId}>
					<h2 id={titleId}>Diálogo con transición</h2>
					{phase === "form" ? (
						// `key` distinto por fase: fuerza a React a desmontar este
						// subárbol entero (en vez de reconciliar "Enviar" con
						// "Reintentar" en la misma posición y conservar el nodo
						// enfocado con una etiqueta nueva) — así se reproduce
						// fielmente que el control enfocado desaparece de verdad,
						// igual que en FeedbackModal/MessageRatingPopover con su
						// `AnimatePresence mode="wait"` por fase.
						<div key="form">
							<button type="button">Primero</button>
							<button type="button" onClick={() => setPhase("success")}>
								Enviar
							</button>
						</div>
					) : (
						<div key="success">
							<button type="button">Reintentar</button>
							<button type="button">Cerrar</button>
						</div>
					)}
				</div>
			)}
		</div>
	);
}

describe("useDialog — primitiva local de diálogo", () => {
	it("al abrir sin initialFocusRef, el foco entra al primer elemento focusable del panel", async () => {
		const user = userEvent.setup();
		render(<Harness />);

		await user.click(screen.getByRole("button", { name: "Abrir" }));

		expect(screen.getByRole("button", { name: "Primero" })).toHaveFocus();
	});

	it("con initialFocusRef, el foco entra a ese elemento en vez del primero", async () => {
		const user = userEvent.setup();
		render(<Harness useInitialFocusRef />);

		await user.click(screen.getByRole("button", { name: "Abrir" }));

		expect(screen.getByRole("button", { name: "Especial" })).toHaveFocus();
		expect(screen.getByRole("button", { name: "Primero" })).not.toHaveFocus();
	});

	it("Tab en el último elemento focusable vuelve al primero (trampa de foco)", async () => {
		const user = userEvent.setup();
		render(<Harness />);

		await user.click(screen.getByRole("button", { name: "Abrir" }));
		expect(screen.getByRole("button", { name: "Primero" })).toHaveFocus();

		await user.tab(); // Primero -> Segundo (orden normal, sin intervención del hook)
		expect(screen.getByRole("button", { name: "Segundo" })).toHaveFocus();

		await user.tab(); // Segundo -> Último
		expect(screen.getByRole("button", { name: "Último" })).toHaveFocus();

		await user.tab(); // Último -> vuelve a Primero (trampa)
		expect(screen.getByRole("button", { name: "Primero" })).toHaveFocus();
	});

	it("Shift+Tab en el primero va al último (trampa de foco en reversa)", async () => {
		const user = userEvent.setup();
		render(<Harness />);

		await user.click(screen.getByRole("button", { name: "Abrir" }));
		expect(screen.getByRole("button", { name: "Primero" })).toHaveFocus();

		await user.tab({ shift: true });
		expect(screen.getByRole("button", { name: "Último" })).toHaveFocus();
	});

	it("Escape cierra el diálogo y devuelve el foco al botón que lo abrió", async () => {
		const user = userEvent.setup();
		render(<Harness />);

		const trigger = screen.getByRole("button", { name: "Abrir" });
		await user.click(trigger);
		expect(screen.getByRole("dialog")).toBeInTheDocument();

		await user.keyboard("{Escape}");

		expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
		expect(trigger).toHaveFocus();
	});

	it("closeBlocked=true: Escape no cierra el diálogo", async () => {
		const user = userEvent.setup();
		render(<Harness closeBlocked />);

		await user.click(screen.getByRole("button", { name: "Abrir" }));
		expect(screen.getByRole("dialog")).toBeInTheDocument();

		await user.keyboard("{Escape}");

		expect(screen.getByRole("dialog")).toBeInTheDocument();
	});

	it("sin controles habilitados, el foco inicial y Tab se quedan en el propio panel", async () => {
		const user = userEvent.setup();
		render(<NoFocusableHarness />);

		await user.click(screen.getByRole("button", { name: "Abrir" }));

		const panel = screen.getByRole("dialog");
		expect(panel).toHaveFocus();

		await user.tab();
		expect(panel).toHaveFocus();
	});

	it("si el control enfocado desaparece y el foco queda fuera del panel, Tab lo recupera hacia el primer control vigente", async () => {
		const user = userEvent.setup();
		render(<TransitionHarness />);

		await user.click(screen.getByRole("button", { name: "Abrir" }));
		await user.click(screen.getByRole("button", { name: "Enviar" }));

		// "Enviar" (que tenía el foco al hacer click) desapareció al cambiar de
		// fase; el navegador (y jsdom) mueven el foco a <body> cuando el
		// elemento enfocado se retira del documento — sin recuperación
		// explícita, el siguiente Tab escaparía del diálogo desde ahí.
		expect(screen.queryByRole("button", { name: "Enviar" })).not.toBeInTheDocument();
		expect(document.activeElement).toBe(document.body);

		// Se dispara el evento directamente (en vez de `user.tab()`) porque el
		// atrapado de foco de este hook es quien decide el destino — no el
		// orden de tabulación nativo simulado por userEvent — y partir de
		// `document.body` (sin una posición previa rastreable) puede hacer que
		// esa simulación no coincida con la dirección solicitada.
		fireEvent.keyDown(document, { key: "Tab", code: "Tab" });
		expect(screen.getByRole("button", { name: "Reintentar" })).toHaveFocus();
	});

	it("el mismo escenario con Shift+Tab recupera el foco hacia el último control vigente", async () => {
		const user = userEvent.setup();
		render(<TransitionHarness />);

		await user.click(screen.getByRole("button", { name: "Abrir" }));
		await user.click(screen.getByRole("button", { name: "Enviar" }));
		expect(document.activeElement).toBe(document.body);

		fireEvent.keyDown(document, { key: "Tab", code: "Tab", shiftKey: true });
		expect(screen.getByRole("button", { name: "Cerrar" })).toHaveFocus();
	});

	it("el foco inicial ya está aplicado cuando el panel aparece en el DOM, aunque la apertura no venga de un evento de usuario", async () => {
		// Reproduce la apertura de la búsqueda desde el panel móvil: llega
		// desde `onExitComplete` de una animación (fuera de un evento de
		// usuario y de `act`). En esa prioridad React difiere los efectos
		// pasivos a una tarea posterior al commit; si el foco inicial se
		// aplicara en un `useEffect`, habría una ventana con el diálogo ya
		// montado y el foco todavía fuera (en <body>). El MutationObserver
		// mira el foco justo después del commit, antes de esa tarea.
		const controls = { open: () => {} };
		function ExternalOpenHarness() {
			const [open, setOpen] = useState(false);
			useEffect(() => {
				controls.open = () => setOpen(true);
			}, []);
			const inputRef = useRef<HTMLInputElement>(null);
			const { panelRef, titleId } = useDialog({
				open,
				onClose: () => setOpen(false),
				initialFocusRef: inputRef,
			});
			return open ? (
				<div ref={panelRef} tabIndex={-1} role="dialog" aria-labelledby={titleId}>
					<h2 id={titleId}>Búsqueda</h2>
					<button type="button">Cerrar</button>
					<input ref={inputRef} aria-label="Buscar" />
				</div>
			) : null;
		}
		render(<ExternalOpenHarness />);

		const actEnvironment = globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean };
		const previous = actEnvironment.IS_REACT_ACT_ENVIRONMENT;
		actEnvironment.IS_REACT_ACT_ENVIRONMENT = false;
		try {
			const focusedWhenPanelAppeared = await new Promise<Element | null>((resolve) => {
				const observer = new MutationObserver(() => {
					if (!document.querySelector('[role="dialog"]')) return;
					observer.disconnect();
					resolve(document.activeElement);
				});
				observer.observe(document.body, { childList: true, subtree: true });
				setTimeout(() => controls.open(), 0);
			});
			expect(focusedWhenPanelAppeared).toBe(screen.getByRole("textbox", { name: "Buscar" }));
		} finally {
			actEnvironment.IS_REACT_ACT_ENVIRONMENT = previous;
		}
	});
});

/**
 * Panel para los escenarios de traspaso. Con `keepMountedWhileExiting`, el
 * nodo sigue en el DOM durante el commit en que `open` pasa a false y se
 * retira en una tarea posterior, como hace `AnimatePresence` con su
 * animación de salida (caso de `MobileSidebar` y `ConversationSearchDialog`).
 */
function HandoffPanel({
	open,
	name,
	onClose,
	keepMountedWhileExiting,
}: {
	open: boolean;
	name: string;
	onClose: () => void;
	keepMountedWhileExiting: boolean;
}) {
	const inputRef = useRef<HTMLInputElement>(null);
	const [exiting, setExiting] = useState(false);
	const [wasOpen, setWasOpen] = useState(open);
	if (open !== wasOpen) {
		setWasOpen(open);
		if (!open && keepMountedWhileExiting) setExiting(true);
	}
	useEffect(() => {
		if (!exiting) return;
		const id = setTimeout(() => setExiting(false), 0);
		return () => clearTimeout(id);
	}, [exiting]);
	const { panelRef, titleId } = useDialog({ open, onClose, initialFocusRef: inputRef });
	if (!open && !exiting) return null;
	return (
		<div
			ref={panelRef}
			tabIndex={-1}
			role="dialog"
			aria-labelledby={titleId}
			data-dialog-name={name}
		>
			<h2 id={titleId}>{name}</h2>
			<input ref={inputRef} aria-label={`campo ${name}`} />
			<button type="button">acción {name}</button>
		</div>
	);
}

/**
 * Dos diálogos: "A" (equivalente al panel móvil) y "B" (equivalente a la
 * búsqueda). Escape en A lo cierra y abre B en el mismo commit. `bFirst`
 * invierte el orden de renderizado (y, por tanto, de sus efectos).
 */
function HandoffPair({
	bFirst,
	keepMountedWhileExiting,
}: {
	bFirst: boolean;
	keepMountedWhileExiting: boolean;
}) {
	const [stage, setStage] = useState<"none" | "a" | "b">("none");
	const [mounted, setMounted] = useState(true);
	const a = (
		<HandoffPanel
			key="a"
			name="A"
			open={stage === "a"}
			onClose={() => setStage("b")}
			keepMountedWhileExiting={keepMountedWhileExiting}
		/>
	);
	const b = (
		<HandoffPanel
			key="b"
			name="B"
			open={stage === "b"}
			onClose={() => setStage("none")}
			keepMountedWhileExiting={keepMountedWhileExiting}
		/>
	);
	return (
		<div>
			<button type="button" onClick={() => setStage("a")}>
				origen
			</button>
			<button type="button" onClick={() => setMounted(false)}>
				desmontar diálogos
			</button>
			{mounted && (bFirst ? [b, a] : [a, b])}
		</div>
	);
}

describe("useDialog — traspaso del disparador entre diálogos en el mismo commit", () => {
	let consoleMessages: unknown[][] = [];
	beforeEach(() => {
		consoleMessages = [];
		vi.spyOn(console, "error").mockImplementation((...args) => consoleMessages.push(args));
		vi.spyOn(console, "warn").mockImplementation((...args) => consoleMessages.push(args));
	});
	afterEach(() => {
		vi.restoreAllMocks();
		// Ningún aviso de React (act, efectos, claves...) en estos escenarios.
		expect(consoleMessages).toEqual([]);
	});

	const scenarios = [
		{ order: "A antes que B (orden de MobileSidebar y la búsqueda)", bFirst: false },
		{ order: "B antes que A (orden inverso)", bFirst: true },
	].flatMap((o) =>
		[false, true].flatMap((keepMountedWhileExiting) =>
			[false, true].map((strict) => ({ ...o, keepMountedWhileExiting, strict })),
		),
	);

	function renderPair(scenario: (typeof scenarios)[number]) {
		const tree: ReactNode = (
			<HandoffPair
				bFirst={scenario.bFirst}
				keepMountedWhileExiting={scenario.keepMountedWhileExiting}
			/>
		);
		return render(scenario.strict ? <StrictMode>{tree}</StrictMode> : tree);
	}

	async function waitForExitsToFinish() {
		await waitFor(() => expect(document.querySelectorAll('[role="dialog"]').length).toBeLessThanOrEqual(1));
	}

	it.each(scenarios)(
		"$order, panel montado durante la salida: $keepMountedWhileExiting, StrictMode: $strict — B recibe el foco, Tab queda dentro y al cerrar vuelve al disparador original",
		async (scenario) => {
			const user = userEvent.setup();
			renderPair(scenario);
			const origin = screen.getByRole("button", { name: "origen" });
			await user.click(origin);
			expect(screen.getByRole("textbox", { name: "campo A" })).toHaveFocus();

			// A se cierra y B se abre en el mismo commit.
			await user.keyboard("{Escape}");
			const inputB = screen.getByRole("textbox", { name: "campo B" });
			expect(inputB).toHaveFocus();
			await waitForExitsToFinish();
			expect(inputB).toHaveFocus();

			const panelB = inputB.closest('[role="dialog"]') as HTMLElement;
			await user.tab();
			expect(panelB.contains(document.activeElement)).toBe(true);
			await user.tab({ shift: true });
			expect(panelB.contains(document.activeElement)).toBe(true);

			const focusSpy = vi.spyOn(origin, "focus");
			await user.keyboard("{Escape}");
			expect(document.activeElement).toBe(origin);
			// Una sola restauración, sin repetirse al terminar la salida.
			await waitFor(() => expect(document.querySelectorAll('[role="dialog"]').length).toBe(0));
			expect(focusSpy).toHaveBeenCalledTimes(1);
			expect(document.activeElement).toBe(origin);
		},
	);

	it.each(scenarios)(
		"$order, panel montado durante la salida: $keepMountedWhileExiting, StrictMode: $strict — desmontar los diálogos con B abierto devuelve el foco al disparador original y el registro no queda obsoleto",
		async (scenario) => {
			const user = userEvent.setup();
			const { unmount } = renderPair(scenario);
			const origin = screen.getByRole("button", { name: "origen" });
			await user.click(origin);
			await user.keyboard("{Escape}");
			expect(screen.getByRole("textbox", { name: "campo B" })).toHaveFocus();
			await waitForExitsToFinish();

			const focusSpy = vi.spyOn(origin, "focus");
			// fireEvent no mueve el foco: B sigue enfocado al desmontarse.
			fireEvent.click(screen.getByRole("button", { name: "desmontar diálogos" }));
			expect(document.activeElement).toBe(origin);
			expect(focusSpy).toHaveBeenCalledTimes(1);
			unmount();

			// Un diálogo nuevo, tras el desmontaje anterior, se comporta igual:
			// ninguna entrada obsoleta del registro le altera el disparador.
			renderPair(scenario);
			const newOrigin = screen.getByRole("button", { name: "origen" });
			await user.click(newOrigin);
			await user.keyboard("{Escape}");
			await waitForExitsToFinish();
			await user.keyboard("{Escape}");
			expect(document.activeElement).toBe(newOrigin);
		},
	);
});
