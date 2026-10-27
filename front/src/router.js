import {createRouter, createWebHistory} from "vue-router";
import AndroidPayTutorial from "@/pages/AndroidPayTutorial.vue";
const routes = [
    {
        path: "/",
        name: "Home",
        component: () => import ("@/pages/Home.vue"),
        meta: {
            title: "自助打印",
        }
    },
    {
        path: "/library",
        name: "Library",
        component: () => import ("@/pages/Library.vue"),
        meta: {
            title: "共享资料库",
        }
    },
    {
        path: "/login",
        name: "Login",
        component: () => import ("@/pages/Login.vue"),
        meta: {
            title: "登录",
        }
    },
    {
        path: "/register",
        name: "Register",
        component: () => import ("@/pages/Register.vue"),
        meta: {
            title: "注册",
        }
    },
    {
        path: "/tutorial/android/pay",
        name: "androidPayTutorial",
        component: AndroidPayTutorial
    }
    // legacy page
    // using '/pdfjs/web/viewer.html?file=' instead
    // {
    //     path: "/preview/:pdfUrl",
    //     name: "preview",
    //     component: Previewer
    // },
]

const router = createRouter({
    history: createWebHistory(),
    routes
})
router.beforeEach((to, from, next) => {
  document.title = to.meta.title;
  next();
})
export default router