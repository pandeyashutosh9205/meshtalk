import tkinter as tk


def main():
    root = tk.Tk()
    root.title("MeshTalk")
    root.geometry("700x450")

    tk.Label(root, text="MeshTalk window works").pack(pady=20)

    root.mainloop()


if __name__ == "__main__":
    main()