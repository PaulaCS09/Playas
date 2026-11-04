"use client";

import { useEffect, useId, useLayoutEffect, useRef } from "react";

const FOCUSABLE_SELECTOR =
	'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Registro interno de los diálogos abiertos (no forma parte de la API). Se
 * usa para traspasar el disparador cuando un diálogo se cierra y otro se
 * abre en el mismo commit: según el orden en que React ejecute sus efectos,
 * el que se abre puede capturar como disparador un elemento del que se
 * cierra, `<body>` o un nodo desconectado. Al cerrarse, cada diálogo sale
 * del registro.
 */
interface OpenDialog {
	panelRef: React.RefObject<HTMLDivElement | null>;
	triggerRef: React.RefObject<Element | null>;
}
const openDialogs = new Set<OpenDialog>();

/** Un disparador sirve para restaurar el foco si sigue en el documento, no es
 *  `<body>` y no pertenece al panel que se está cerrando. */
function isUsableTrigger(element: Element | null, closingPanel: HTMLElement | null) {
	return (
		element instanceof HTMLElement &&
		element !== document.body &&
		element.isConnected &&
		!(closingPanel?.contains(element) ?? false)
	);
}

export interface UseDialogOptions {
	/** true mientras el diálogo está abierto. */
	open: boolean;
	/** Se invoca para cerrar (Escape). Nunca se llama mientras `closeBlocked` sea true. */
	onClose: () => void;
	/**
	 * Bloquea el cierre por Escape mientras una operación crítica está en
	 * curso (p. ej. un envío). El backdrop y el botón de cierre explícito de
	 * cada consumidor deben respetar el mismo valor por su cuenta.
	 */
	closeBlocked?: boolean;
	/** Enfocar este elemento al abrir, en vez del primer elemento focusable del panel. */
	initialFocusRef?: React.RefObject<HTMLElement | null>;
}

export interface UseDialogResult {
	/**
	 * Debe colocarse en el contenedor del panel (el elemento con
	 * role="dialog"). El consumidor debe darle también `tabIndex={-1}`: es
	 * el respaldo de foco cuando el panel no tiene ningún control habilitado
	 * (p. ej. durante un envío) y el objetivo al que este hook recupera el
	 * foco si, por cualquier razón, queda fuera del panel.
	 */
	panelRef: React.RefObject<HTMLDivElement | null>;
	/** Id único (vía `useId`) para `aria-labelledby` en el panel y `id` en el título visible. */
	titleId: string;
}

/**
 * Primitiva local compartida de comportamiento de diálogo modal.
 *
 * Cubre exactamente lo que hoy está duplicado (de forma parcial e
 * inconsistente) entre `AuthModal`, `FeedbackModal`, `MessageRatingPopover`,
 * `ConversationSearchDialog` y el modal administrativo de usuarios: atrapa
 * el foco dentro del panel (Tab/Shift+Tab), cierra con Escape, enfoca el
 * panel al abrir y devuelve el foco al elemento que lo disparó al cerrar.
 *
 * No impone marcado, estilos ni animación — cada consumidor conserva su
 * propio backdrop/panel/transición de Motion tal cual está hoy; solo debe
 * envolver su panel con `panelRef` (más `tabIndex={-1}`, ver arriba) y usar
 * `titleId` para `aria-labelledby` en el panel y como `id` del título
 * visible. El cierre por click en el backdrop y el botón de cierre
 * explícito quedan a cargo de cada consumidor (según si aplica
 * `closeOnBackdrop`/`dismissible` en ese caso), ya que su marcado ya varía
 * entre ellos.
 */
export function useDialog({
	open,
	onClose,
	closeBlocked = false,
	initialFocusRef,
}: UseDialogOptions): UseDialogResult {
	const titleId = useId();
	const panelRef = useRef<HTMLDivElement>(null);
	const triggerRef = useRef<Element | null>(null);

	// Foco inicial al abrir, y retorno del foco al disparador al cerrar (o al
	// desmontar mientras estaba abierto).
	//
	// - `useLayoutEffect`, no `useEffect`: el foco inicial se aplica en el
	//   mismo commit que monta el panel. Con `useEffect`, si la apertura no
	//   viene de un evento de usuario (p. ej. la búsqueda abierta desde
	//   `onExitComplete` de la animación del panel móvil), React difiere el
	//   efecto a una tarea posterior y queda una ventana con el diálogo ya
	//   visible y el foco fuera (en <body>).
	// - La restauración al cerrar se hace en el cuerpo del efecto (fase de
	//   layout) y no en su limpieza: ReactDOM restaura, al terminar las
	//   mutaciones de cada commit, el foco que había antes si ese elemento
	//   sigue en el documento, y las limpiezas de layout corren durante esas
	//   mutaciones. Un panel que sigue montado animando su salida recuperaría
	//   el foco.
	const wasOpenRef = useRef(false);
	const entryRef = useRef<OpenDialog | null>(null);

	function restoreFocusToTrigger() {
		wasOpenRef.current = false;
		if (entryRef.current) openDialogs.delete(entryRef.current);
		entryRef.current = null;
		const trigger = triggerRef.current;
		triggerRef.current = null;
		const panel = panelRef.current;
		const active = document.activeElement;

		// Traspaso: otro diálogo abierto que capturó como disparador algo que
		// ya no sirve (un elemento de este panel, `<body>` o un nodo
		// desconectado) recibe el disparador original de este, que sí sirve.
		if (isUsableTrigger(trigger, panel)) {
			for (const other of openDialogs) {
				if (!isUsableTrigger(other.triggerRef.current, panel)) {
					other.triggerRef.current = trigger;
				}
			}
		}

		// No se roba el foco que otro diálogo ya colocó en otro sitio (p. ej.
		// la búsqueda que se abre mientras se cierra el panel móvil): solo se
		// restaura si el foco sigue en este panel, en <body> o en un nodo que
		// ya no está en el documento.
		const focusIsOurs =
			!active ||
			active === document.body ||
			!active.isConnected ||
			(panel?.contains(active) ?? false);
		if (focusIsOurs && trigger instanceof HTMLElement && trigger.isConnected) {
			trigger.focus();
		}
	}

	useLayoutEffect(() => {
		if (open) {
			wasOpenRef.current = true;
			triggerRef.current = document.activeElement;
			entryRef.current = { panelRef, triggerRef };
			openDialogs.add(entryRef.current);
			const target =
				initialFocusRef?.current ??
				panelRef.current?.querySelector<HTMLElement>(FOCUSABLE_SELECTOR) ??
				panelRef.current;
			target?.focus();
			return;
		}
		if (wasOpenRef.current) restoreFocusToTrigger();
		// Solo debe re-ejecutarse cuando cambia `open`: `initialFocusRef` es una
		// ref estable y no debe disparar un nuevo ciclo de foco.
		// eslint-disable-next-line react-hooks/exhaustive-deps
	}, [open]);

	// Desmontaje con el diálogo abierto: el elemento enfocado sale del
	// documento, así que ReactDOM no revierte esta restauración.
	useLayoutEffect(
		() => () => {
			if (wasOpenRef.current) restoreFocusToTrigger();
		},
		[],
	);

	// Escape para cerrar, y Tab/Shift+Tab contenidos dentro del panel.
	useEffect(() => {
		if (!open) return;

		function handleKeyDown(event: KeyboardEvent) {
			if (event.key === "Escape") {
				if (closeBlocked) return;
				event.preventDefault();
				onClose();
				return;
			}
			if (event.key !== "Tab") return;
			const panel = panelRef.current;
			if (!panel) return;
			const focusable = Array.from(
				panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
			);

			// Sin ningún control habilitado (p. ej. todo el formulario
			// deshabilitado durante un envío), no hay nada a lo que atrapar el
			// foco: se mantiene en el propio panel (que el consumidor vuelve
			// focuseable con `tabIndex={-1}`).
			if (focusable.length === 0) {
				event.preventDefault();
				panel.focus();
				return;
			}

			const first = focusable[0];
			const last = focusable[focusable.length - 1];
			const active = document.activeElement;

			// Si el foco quedó fuera del panel (por ejemplo, el control que
			// tenía el foco desapareció al cambiar de una rama de contenido a
			// otra dentro del mismo diálogo, como formulario → éxito, y el
			// navegador lo movió a `<body>`), Tab/Shift+Tab ya no encontraría
			// nunca `first`/`last` como `document.activeElement` y el atrapado
			// de foco se rompería en silencio. Se recupera explícitamente hacia
			// el primer o último control según la dirección.
			const focusIsInsidePanel = active instanceof Node && panel.contains(active);
			if (!focusIsInsidePanel) {
				event.preventDefault();
				(event.shiftKey ? last : first).focus();
				return;
			}

			if (event.shiftKey && active === first) {
				event.preventDefault();
				last.focus();
			} else if (!event.shiftKey && active === last) {
				event.preventDefault();
				first.focus();
			}
		}

		document.addEventListener("keydown", handleKeyDown);
		return () => document.removeEventListener("keydown", handleKeyDown);
	}, [open, closeBlocked, onClose]);

	return { panelRef, titleId };
}
